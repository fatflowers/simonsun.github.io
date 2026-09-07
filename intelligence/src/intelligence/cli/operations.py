"""Operational command helpers kept separate from argument parsing.

The control plane deliberately separates planning from ingestion: Multica/Codex
may call an approved MCP tool, but the returned JSON must pass through the fixed
adapter and deterministic storage payload builder here.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import subprocess
import tempfile
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

import yaml

from intelligence.analyzer import validate_analysis
from intelligence.catalog import CatalogError, CatalogRepository
from intelligence.collectors import ChannelSpec, CollectorRouter, GitHubCollector, MCPRegistryCollector, RouteStep
from intelligence.collectors.adapters import get_adapter
from intelligence.collectors.github import environment_token
from intelligence.collectors.http import HTTPCollector, WebDiffCollector
from intelligence.collectors.rss import RSSCollector
from intelligence.mcp import MCPToolRegistry
from intelligence.models.catalog import stable_id
from intelligence.normalize import NormalizedItem, content_hash, dedupe_key
from intelligence.publisher import GitPublisher, PublicationService, PublishValidator
from intelligence.reporter import (
    Report,
    ReportEdition,
    ReportPolicy,
    ReportSignal,
    ReportSource,
    ReportStatus,
    render_hugo_report,
)
from intelligence.storage import WorkerAPIClient
from intelligence.reporter.editorial import exclusion_reason, verified_observed_change


ITEM_NAMESPACE = UUID("2544494d-31e0-4b6a-9e18-2ad2dd2361ed")
REPORT_NAMESPACE = UUID("19607466-fb10-4678-a786-64cd13cccd4e")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json_input(path: Optional[Path]) -> Any:
    if path is None or str(path) == "-":
        import sys

        source = sys.stdin.read()
    else:
        source = path.read_text(encoding="utf-8")
    if not source.strip():
        raise CatalogError("JSON input is empty")
    try:
        return json.loads(source)
    except json.JSONDecodeError as exc:
        raise CatalogError("invalid JSON input: %s" % exc) from exc


def repository_root(repository: CatalogRepository) -> Path:
    candidates = [repository.path.resolve(), Path.cwd().resolve()]
    for start in candidates:
        directory = start if start.is_dir() else start.parent
        for candidate in (directory, *directory.parents):
            if (candidate / "hugo.toml").exists() and (candidate / "intelligence").is_dir():
                return candidate
    raise CatalogError("cannot locate repository root containing hugo.toml")


def registry_path(repository: CatalogRepository) -> Path:
    configured = os.getenv("INTELLIGENCE_MCP_REGISTRY_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return repository_root(repository) / "intelligence" / "config" / "mcp-tools.yaml"


def list_mcp_bindings(repository: CatalogRepository) -> Dict[str, Any]:
    path = registry_path(repository)
    registry = MCPToolRegistry.load(path)
    return {
        "server": registry.server_name,
        "path": str(path),
        "bindings": [
            {
                "alias": alias,
                "status": binding.status,
                "tool_name": binding.tool_name,
                "channel_types": list(binding.channel_types),
                "output_adapter": binding.output_adapter,
                "contract_version": binding.contract_version,
            }
            for alias, binding in sorted(registry.bindings.items())
        ],
    }


def show_mcp_binding(repository: CatalogRepository, alias: str) -> Dict[str, Any]:
    path = registry_path(repository)
    raw = _load_registry_raw(path)
    tools = raw.get("tools", {})
    if not isinstance(tools, Mapping) or alias not in tools:
        raise CatalogError("MCP binding not found: %s" % alias)
    # Validate the full registry before showing a possibly malformed entry.
    MCPToolRegistry.from_mapping(raw)
    return {"alias": alias, "binding": dict(tools[alias])}


def verify_mcp_binding(
    repository: CatalogRepository,
    alias: str,
    evidence: str,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    evidence = evidence.strip()
    if not evidence:
        raise CatalogError("--evidence must be a non-empty verification summary")
    if len(evidence) > 2000:
        raise CatalogError("--evidence cannot exceed 2000 characters")
    if re.search(
        r"(?i)(authorization\s*:\s*bearer|api[_-]?key\s*[:=]|access[_-]?token\s*[:=]|gh[pousr]_|sk-[a-z0-9_-]{12,})",
        evidence,
    ):
        raise CatalogError("--evidence appears to contain a credential")

    path = registry_path(repository)
    raw = _load_registry_raw(path)
    tools = raw.get("tools", {})
    if not isinstance(tools, dict) or alias not in tools or not isinstance(tools[alias], dict):
        raise CatalogError("MCP binding not found: %s" % alias)
    binding = tools[alias]
    current = binding.get("status")
    if current != "schema_verified":
        raise CatalogError(
            "binding %s is %s; only schema_verified can transition to verified"
            % (alias, current)
        )
    before = {"status": current, "contract": dict(binding.get("contract") or {})}
    binding["status"] = "verified"
    contract = binding.setdefault("contract", {})
    if not isinstance(contract, dict):
        raise CatalogError("binding contract must be an object: %s" % alias)
    contract["verified_at"] = utc_now()
    contract["evidence"] = evidence
    MCPToolRegistry.from_mapping(raw)
    if not dry_run:
        _atomic_yaml_write(path, raw)
    return {
        "alias": alias,
        "dry_run": dry_run,
        "before": before,
        "after": {"status": "verified", "contract": dict(contract)},
    }


def _load_registry_raw(path: Path) -> Dict[str, Any]:
    from intelligence.catalog.repository import UniqueKeySafeLoader

    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise CatalogError("invalid MCP registry YAML: %s" % exc) from exc
    if not isinstance(raw, dict):
        raise CatalogError("MCP registry root must be an object")
    return raw


def _atomic_yaml_write(path: Path, value: Mapping[str, Any]) -> None:
    rendered = yaml.safe_dump(
        dict(value), allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name,
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        original_mode = path.stat().st_mode & 0o777
        os.chmod(temporary_name, original_mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _raw_channel(
    repository: CatalogRepository, channel_slug: str
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    value = repository.load_raw()
    for target in value.get("targets", []):
        for channel in target.get("channels", []):
            if channel.get("slug") == channel_slug:
                return target, channel
    raise CatalogError("channel not found: %s" % channel_slug)


def _plan_one(
    repository: CatalogRepository,
    registry: MCPToolRegistry,
    target: Mapping[str, Any],
    channel: Mapping[str, Any],
    cursor: Optional[Mapping[str, Any]] = None,
    *,
    scheduled: bool,
) -> Dict[str, Any]:
    spec = ChannelSpec.from_catalog(target, channel)
    result: Dict[str, Any] = {
        "target": spec.target_slug,
        "target_id": stable_id("target", spec.target_slug),
        "channel": spec.channel_slug,
        "channel_id": stable_id("channel", spec.channel_slug),
        "collector": spec.collector_type,
        "url": spec.url,
    }
    if spec.collector_type != "mcp":
        result["action"] = "local_collector"
        return result
    if not spec.tool_binding:
        raise CatalogError("MCP channel has no fixed binding: %s" % spec.channel_slug)
    binding = registry.get(spec.tool_binding)
    binding.assert_runnable(spec.channel_type, scheduled=scheduled)
    result.update(
        {
            "action": "call_mcp_tool",
            "mcp_server": registry.server_name,
            "binding": binding.alias,
            "tool_name": binding.tool_name,
            "arguments": binding.render_arguments(spec.template_context(), cursor),
            "output_adapter": binding.output_adapter,
            "contract_version": binding.contract_version,
        }
    )
    return result


def collection_plan(
    repository: CatalogRepository,
    client: WorkerAPIClient,
    *,
    due: bool,
    target_slug: Optional[str],
    channel_slug: Optional[str],
    limit: int,
    command_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    repository.load()  # validate before any remote query
    registry = MCPToolRegistry.load(registry_path(repository))
    raw = repository.load_raw()
    selected: list[Tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]] = []

    if due:
        if command_run_id:
            now = utc_now()
            client.create_run(
                {
                    "id": command_run_id,
                    "run_type": "collect_plan",
                    "trigger_type": "scheduler",
                    "run_status": "running",
                    "started_at": now,
                    "created_at": now,
                },
                idempotency_key="run:create:%s" % command_run_id,
            )
        target_id = stable_id("target", target_slug) if target_slug else None
        try:
            response = client.get_due_channels(limit=limit, target_id=target_id)
        except Exception as exc:
            if command_run_id:
                _fail_run(client, command_run_id, "due_channel_query_failed", str(exc))
            raise
        due_rows = response.get("channels", [])
        for row in due_rows:
            target, channel = _raw_channel(repository, str(row["slug"]))
            cursor = _json_object(row.get("cursor_json"))
            selected.append((target, channel, cursor))
    elif channel_slug:
        target, channel = _raw_channel(repository, channel_slug)
        selected.append((target, channel, {}))
    elif target_slug:
        target = next(
            (item for item in raw["targets"] if item.get("slug") == target_slug), None
        )
        if target is None:
            raise CatalogError("target not found: %s" % target_slug)
        selected.extend(
            (target, channel, {})
            for channel in target.get("channels", [])
            if channel.get("enabled", True)
        )
    else:
        raise CatalogError("select --due, --target, or --channel")

    plans: list[Dict[str, Any]] = []
    blocked: list[Dict[str, Any]] = []
    for target, channel, cursor in selected[:limit]:
        try:
            plans.append(
                _plan_one(
                    repository,
                    registry,
                    target,
                    channel,
                    cursor,
                    scheduled=due,
                )
            )
        except Exception as exc:
            blocked.append({"channel": channel.get("slug"), "reason": str(exc)})
    if due and command_run_id:
        status = "failed" if blocked and not plans else ("succeeded" if plans else "skipped")
        update: Dict[str, Any] = {
            "run_status": status,
            "item_count": len(plans),
            "metadata": {"blocked": len(blocked)},
        }
        if status == "skipped":
            update["metadata"]["reason"] = "no_due_channels"
        if status == "failed":
            update["error_code"] = "all_due_channels_blocked"
            update["error_summary"] = str(blocked[0].get("reason", "blocked"))[:2000]
        client.update_run(
            command_run_id,
            update,
            idempotency_key="run:finish:%s" % command_run_id,
        )
    return {
        "pipeline_run_id": command_run_id if due else None,
        "plans": plans,
        "blocked": blocked,
        "count": len(plans),
    }


def ingest_collection(
    repository: CatalogRepository,
    client: WorkerAPIClient,
    *,
    channel_slug: str,
    payload: Any,
    command_run_id: str,
    external_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    target, channel = _raw_channel(repository, channel_slug)
    spec = ChannelSpec.from_catalog(target, channel)
    if spec.collector_type != "mcp" or not spec.tool_binding:
        raise CatalogError("collect ingest currently accepts fixed MCP channels only")
    registry = MCPToolRegistry.load(registry_path(repository))
    binding = registry.get(spec.tool_binding)
    binding.assert_runnable(spec.channel_type, scheduled=False)
    adapter = get_adapter(binding.output_adapter)
    items, next_cursor = adapter(payload, spec)

    target_id = stable_id("target", spec.target_slug)
    channel_id = stable_id("channel", spec.channel_slug)
    now = utc_now()
    if bool(spec.config.get("diff")):
        remote = client.get_catalog()
        remote_channel = next((row for row in remote.get("channels", []) if row.get("slug") == channel_slug), {})
        previous = _json_object(remote_channel.get("cursor_json"))
        if len(items) > 1:
            raise CatalogError("page-diff collection requires a single page snapshot")
        if items:
            item = items[0]
            digest = content_hash(item)
            after = item.content_text[:12000]
            before = previous.get("content_excerpt")
            next_cursor = {"content_hash": digest, "content_excerpt": after, "observed_at": now}
            if not previous.get("content_hash") or previous.get("content_hash") == digest:
                items = []
            else:
                diff = {"before_hash": previous["content_hash"], "after_hash": digest,
                        "before_text": before, "after_text": after, "observed_at": now,
                        "previous_observed_at": previous.get("observed_at"), "excerpt_limit": 12000}
                items = [replace(item, metadata={**item.metadata, "web_diff": diff, "date_kind": "observed_change"})]
    records = [
        normalized_item_record(item, target_id=target_id, channel_id=channel_id, now=now)
        for item in items
    ]
    run_id = external_run_id or command_run_id
    if external_run_id is None:
        client.create_run(
            {
                "id": run_id,
                "run_type": "collect",
                "trigger_type": "multica",
                "target_id": target_id,
                "channel_id": channel_id,
                "run_status": "running",
                "started_at": now,
                "created_at": now,
                "metadata": {"binding": binding.alias},
            },
            idempotency_key="run:create:%s" % run_id,
        )
    try:
        response = _write_item_batches(
            client,
            records,
            channel_state={
                "channel_id": channel_id,
                "last_checked_at": now,
                "succeeded": True,
                "cursor": next_cursor,
            },
            idempotency_prefix="items:%s" % run_id,
        )
        client.update_run(
            run_id,
            {"run_status": "succeeded", "item_count": len(records)},
            idempotency_key="run:finish:%s" % run_id,
        )
    except Exception as exc:
        _fail_run(client, run_id, "collection_ingest_failed", str(exc))
        raise
    return {"pipeline_run_id": run_id, "normalized": len(records), "storage": response}


def collect_local(
    repository: CatalogRepository,
    client: WorkerAPIClient,
    *,
    command_run_id: str,
    due: bool,
    channel_slug: Optional[str],
    external_run_id: Optional[str] = None,
    limit: int = 100,
    collectors: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute local collectors and atomically persist cursor/health state."""

    if due == bool(channel_slug):
        raise CatalogError("select exactly one of --due or --channel")
    repository.load()
    raw = repository.load_raw()
    selections: list[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]] = []
    if due:
        response = client.get_due_channels(limit=limit)
        for row in response.get("channels", []):
            if str(row.get("collector_type")) not in {"rss", "http", "github_api", "mcp_registry_api"}:
                continue
            target, channel = _raw_channel(repository, str(row["slug"]))
            selections.append((target, channel, _json_object(row.get("cursor_json"))))
    else:
        assert channel_slug is not None
        target, channel = _raw_channel(repository, channel_slug)
        remote = client.get_catalog()
        row = next(
            (
                item
                for item in remote.get("channels", [])
                if str(item.get("slug")) == channel_slug
            ),
            {},
        )
        selections.append((target, channel, _json_object(row.get("cursor_json"))))

    run_id = external_run_id or command_run_id
    now = utc_now()
    if external_run_id is None:
        target_id = (
            stable_id("target", str(selections[0][0]["slug"]))
            if len(selections) == 1
            else None
        )
        channel_id = (
            stable_id("channel", str(selections[0][1]["slug"]))
            if len(selections) == 1
            else None
        )
        client.create_run(
            {
                "id": run_id,
                "run_type": "collect_local",
                "trigger_type": "scheduler" if due else "multica",
                "target_id": target_id,
                "channel_id": channel_id,
                "run_status": "running",
                "started_at": now,
                "created_at": now,
            },
            idempotency_key="run:create:%s" % run_id,
        )

    if not selections:
        client.update_run(
            run_id,
            {
                "run_status": "skipped",
                "item_count": 0,
                "metadata": {"reason": "no_due_local_channels"},
            },
            idempotency_key="run:skip:%s" % run_id,
        )
        return {
            "pipeline_run_id": run_id,
            "status": "skipped",
            "reason": "no_due_local_channels",
            "channels": [],
        }

    results = []
    failures = []
    total = 0
    for target, channel, cursor in selections:
        try:
            result = _collect_local_channel(
                client,
                target=target,
                channel=channel,
                cursor=cursor,
                run_id=run_id,
                collectors=collectors,
            )
            total += int(result["normalized"])
            results.append(result)
        except Exception as exc:
            failures.append({"channel": channel.get("slug"), "error": str(exc)[:500]})

    if failures:
        client.update_run(
            run_id,
            {
                "run_status": "failed",
                "item_count": total,
                "error_code": "local_collection_failed",
                "error_summary": failures[0]["error"],
                "metadata": {"succeeded_channels": len(results), "failed_channels": failures},
            },
            idempotency_key="run:fail:%s" % run_id,
        )
        status = "failed"
    else:
        client.update_run(
            run_id,
            {
                "run_status": "succeeded",
                "item_count": total,
                "metadata": {"succeeded_channels": len(results)},
            },
            idempotency_key="run:finish:%s" % run_id,
        )
        status = "succeeded"
    return {
        "pipeline_run_id": run_id,
        "status": status,
        "item_count": total,
        "channels": results,
        "failures": failures,
    }


def _collect_local_channel(
    client: WorkerAPIClient,
    *,
    target: Mapping[str, Any],
    channel: Mapping[str, Any],
    cursor: Mapping[str, Any],
    run_id: str,
    collectors: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    spec = ChannelSpec.from_catalog(target, channel)
    collector_map: Dict[str, Any] = dict(collectors or {})
    if not collector_map:
        page_collector = HTTPCollector()
        collector_map = {
            "rss": RSSCollector(),
            "http": WebDiffCollector(page_collector)
            if bool(spec.config.get("diff"))
            else page_collector,
            "github_api": GitHubCollector(token_provider=environment_token()),
            "mcp_registry_api": MCPRegistryCollector(),
        }
    fallback_steps = []
    for fallback in channel.get("fallbacks", []):
        if not isinstance(fallback, Mapping) or not fallback.get("collector"):
            continue
        kind = str(fallback["collector"])
        overrides = {key: value for key, value in fallback.items() if key != "collector"}
        fallback_steps.append(RouteStep(kind, overrides or None))
    if spec.collector_type in collector_map:
        route = [RouteStep(spec.collector_type), *fallback_steps]
    else:
        # An explicit local execution of an MCP channel starts at its declared
        # local fallback; it never fabricates or dynamically discovers a tool.
        route = fallback_steps
    if not route:
        raise CatalogError("channel has no executable local collector: %s" % spec.channel_slug)

    now = utc_now()
    try:
        page = CollectorRouter(collector_map).collect(spec, cursor, route=route)
    except Exception as exc:
        try:
            client.write_items(
                [],
                channel_state={
                    "channel_id": stable_id("channel", spec.channel_slug),
                    "last_checked_at": now,
                    "succeeded": False,
                    "error_summary": str(exc)[:2000],
                },
                idempotency_key="items:%s:%s:failed" % (run_id, spec.channel_slug),
            )
        except Exception:
            pass
        raise

    target_id = stable_id("target", spec.target_slug)
    channel_id = stable_id("channel", spec.channel_slug)
    records = [
        normalized_item_record(item, target_id=target_id, channel_id=channel_id, now=now)
        for item in page.items
    ]
    storage = _write_item_batches(
        client,
        records,
        channel_state={
            "channel_id": channel_id,
            "last_checked_at": now,
            "succeeded": True,
            "cursor": page.next_cursor,
        },
        idempotency_prefix="items:%s:%s" % (run_id, spec.channel_slug),
    )
    return {
        "channel": spec.channel_slug,
        "collector": page.metadata.get("collector_type", spec.collector_type),
        "normalized": len(records),
        "raw_count": page.raw_count,
        "cursor": dict(page.next_cursor),
        "storage": storage,
    }


def _write_item_batches(
    client: WorkerAPIClient,
    records: Sequence[Mapping[str, Any]],
    *,
    channel_state: Mapping[str, Any],
    idempotency_prefix: str,
) -> Dict[str, Any]:
    """Persist at most 100 items per request; commit cursor only on the final batch."""

    chunks = [list(records[index : index + 100]) for index in range(0, len(records), 100)]
    if not chunks:
        chunks = [[]]
    responses = []
    for index, chunk in enumerate(chunks):
        responses.append(
            client.write_items(
                chunk,
                channel_state=channel_state if index == len(chunks) - 1 else None,
                idempotency_key="%s:%d" % (idempotency_prefix, index),
            )
        )
    return {
        "batches": len(responses),
        "accepted": sum(int(value.get("accepted", len(chunks[index]))) for index, value in enumerate(responses)),
        "inserted": sum(int(value.get("inserted", 0)) for value in responses),
        "duplicates": sum(int(value.get("duplicates", 0)) for value in responses),
        "channel_state_updated": bool(responses[-1].get("channel_state_updated", True)),
    }


def normalized_item_record(
    item: NormalizedItem, *, target_id: str, channel_id: str, now: str
) -> Dict[str, Any]:
    key_type, key_value = dedupe_key(item)
    metadata = dict(item.metadata)
    if "web_diff" in metadata:
        metadata["date_kind"] = "observed_change"
    diff = verified_observed_change(metadata)
    if diff:
        key_type, key_value = "observed_change", "%s:%s:%s" % (channel_id, item.canonical_url, diff["after_hash"])
        metadata["date_kind"] = "observed_change"
    return {
        "id": str(uuid5(ITEM_NAMESPACE, "%s:%s" % (key_type, key_value))),
        "target_id": target_id,
        "channel_id": channel_id,
        "external_id": "diff:%s" % diff["after_hash"] if diff else item.external_id,
        "url": item.url,
        "canonical_url": item.canonical_url,
        "title": item.title,
        "author": item.author,
        "published_at": diff["observed_at"] if diff else item.published_at,
        "fetched_at": item.fetched_at or now,
        "content_text": item.content_text,
        "content_hash": content_hash(item),
        "language": item.language,
        "raw_metadata": metadata,
        "created_at": now,
    }


def pending_analysis(
    client: WorkerAPIClient,
    *,
    command_run_id: str,
    limit: int,
    target_slug: Optional[str],
    channel_slug: Optional[str],
    since: Optional[str] = None,
) -> Dict[str, Any]:
    now = utc_now()
    client.create_run(
        {
            "id": command_run_id,
            "run_type": "analyze",
            "trigger_type": "multica",
            "target_id": stable_id("target", target_slug) if target_slug else None,
            "channel_id": stable_id("channel", channel_slug) if channel_slug else None,
            "run_status": "running",
            "started_at": now,
            "created_at": now,
        },
        idempotency_key="run:create:%s" % command_run_id,
    )
    try:
        response = client.get_pending_analysis(
            limit=limit,
            target_id=stable_id("target", target_slug) if target_slug else None,
            channel_id=stable_id("channel", channel_slug) if channel_slug else None,
            since=since,
        )
    except Exception as exc:
        _fail_run(client, command_run_id, "pending_analysis_query_failed", str(exc))
        raise
    items = response.get("items", [])
    if not items:
        client.update_run(
            command_run_id,
            {
                "run_status": "skipped",
                "item_count": 0,
                "metadata": {"reason": "no_pending_items"},
            },
            idempotency_key="run:skip:%s" % command_run_id,
        )
    return {
        "pipeline_run_id": command_run_id,
        "status": "running" if items else "skipped",
        "items": items,
        "recent_published_events": response.get("recent_published_events", []),
    }


def ingest_analyses(
    client: WorkerAPIClient,
    *,
    payload: Any,
    command_run_id: str,
    external_run_id: Optional[str],
    model: str,
    prompt_version: str,
) -> Dict[str, Any]:
    raw_values = payload.get("analyses") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_values, list):
        raise CatalogError("analysis input must be an array or an object with analyses")
    now = utc_now()
    records = []
    for index, raw in enumerate(raw_values):
        if not isinstance(raw, Mapping):
            raise CatalogError("analyses[%d] must be an object" % index)
        item_id = raw.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            raise CatalogError("analyses[%d].item_id is required" % index)
        analysis_payload = {
            key: raw[key]
            for key in (
                "headline",
                "summary",
                "key_change",
                "why_it_matters",
                "company_impact",
                "importance",
                "confidence",
                "topics",
                "watch_next",
                "evidence",
            )
            if key in raw
        }
        analysis = validate_analysis(analysis_payload)
        record = analysis.to_dict()
        record.update(
            {
                "item_id": item_id,
                "model": str(raw.get("model") or model),
                "prompt_version": str(raw.get("prompt_version") or prompt_version),
                "analyzed_at": str(raw.get("analyzed_at") or now),
            }
        )
        if "content_revision" in raw:
            record["content_revision"] = raw["content_revision"]
        records.append(record)
    if len(records) > 100:
        raise CatalogError("one analysis batch cannot exceed 100 records")

    run_id = external_run_id or command_run_id
    if external_run_id is None:
        client.create_run(
            {
                "id": run_id,
                "run_type": "analyze",
                "trigger_type": "multica",
                "run_status": "running",
                "started_at": now,
                "created_at": now,
            },
            idempotency_key="run:create:%s" % run_id,
        )
    try:
        response = client.write_analyses(
            records, idempotency_key="analyses:%s" % run_id
        )
        client.update_run(
            run_id,
            {"run_status": "succeeded", "item_count": len(records)},
            idempotency_key="run:finish:%s" % run_id,
        )
    except Exception as exc:
        _fail_run(client, run_id, "analysis_ingest_failed", str(exc))
        raise
    return {"pipeline_run_id": run_id, "validated": len(records), "storage": response}


def report_window(
    edition: ReportEdition,
    report_date: date,
    *,
    from_value: Optional[str] = None,
    to_value: Optional[str] = None,
) -> Tuple[datetime, datetime]:
    if bool(from_value) != bool(to_value):
        raise CatalogError("--from and --to must be supplied together")
    if from_value and to_value:
        start, end = _datetime(from_value), _datetime(to_value)
        if start >= end:
            raise CatalogError("--from must be earlier than --to")
        return start, end
    zone = ZoneInfo("Asia/Shanghai")
    if edition is ReportEdition.MORNING:
        return (
            datetime.combine(report_date - timedelta(days=1), time(18, 45), zone),
            datetime.combine(report_date, time(8, 15), zone),
        )
    if edition is ReportEdition.MIDDAY:
        return (
            datetime.combine(report_date, time(8, 15), zone),
            datetime.combine(report_date, time(12, 45), zone),
        )
    if edition is ReportEdition.EVENING:
        return (
            datetime.combine(report_date, time(8, 15), zone),
            datetime.combine(report_date, time(18, 45), zone),
        )
    end = datetime.combine(report_date, time(20), zone)
    if edition is ReportEdition.WEEKLY:
        return end - timedelta(days=7), end
    return end - timedelta(days=1), end


def build_report(
    client: WorkerAPIClient,
    *,
    edition_value: str,
    date_value: Optional[str],
    from_value: Optional[str],
    to_value: Optional[str],
    title: Optional[str],
    description: Optional[str],
    trends: Sequence[str],
    tag: Optional[str],
    target_slug: Optional[str],
) -> Tuple[Report, Any, Any]:
    edition = ReportEdition(edition_value)
    zone = ZoneInfo("Asia/Shanghai")
    report_date = date.fromisoformat(date_value) if date_value else datetime.now(zone).date()
    start, end = report_window(
        edition, report_date, from_value=from_value, to_value=to_value
    )
    # Allow late-collected, as-yet-unreported events to be useful. Their true
    # event date remains visible and the renderer labels these as catch-up.
    research_start = start if from_value or edition in {ReportEdition.WEEKLY, ReportEdition.AD_HOC} else (end - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    response = client.get_report_input(
        window_start=research_start.isoformat(),
        window_end=end.isoformat(),
        min_importance=1,
        target_id=stable_id("target", target_slug) if target_slug else None,
        tag=tag,
        include_reported=edition in {ReportEdition.WEEKLY, ReportEdition.AD_HOC},
    )
    signals = tuple(
        _report_signal(row) for row in response.get("items", [])
        if exclusion_reason(row, research_start, end) is None
    )
    decision = ReportPolicy().decide(edition, signals)
    period = (
        "%d-w%02d" % (report_date.isocalendar()[0], report_date.isocalendar()[1])
        if edition is ReportEdition.WEEKLY
        else report_date.isoformat()
    )
    identity = (
        "%s:%s:%s" % (edition.value, start.isoformat(), end.isoformat())
        if edition is ReportEdition.AD_HOC
        else "%s:%s" % (edition.value, period)
    )
    report_id = str(uuid5(REPORT_NAMESPACE, identity))
    label = {
        ReportEdition.MORNING: "AI 情报早报",
        ReportEdition.MIDDAY: "AI 情报午间快讯",
        ReportEdition.EVENING: "AI 情报晚报",
        ReportEdition.WEEKLY: "AI 战略情报周报",
        ReportEdition.AD_HOC: "AI 专题情报",
    }[edition]
    generated_at = datetime.combine(
        report_date,
        {
            ReportEdition.MORNING: time(8, 30),
            ReportEdition.MIDDAY: time(13, 0),
            ReportEdition.EVENING: time(19, 0),
            ReportEdition.WEEKLY: time(20, 0),
            ReportEdition.AD_HOC: time(20, 0),
        }[edition],
        zone,
    )
    report = Report(
        report_id=report_id,
        edition=edition,
        period=period,
        generated_at=generated_at,
        window_start=start,
        window_end=end,
        title=title or "%s｜%s" % (label, period),
        description=description or "公开来源中的 AI、Agent 与开发者平台重要变化。",
        signals=decision.selected,
        trends=tuple(trends),
    )
    rendered = render_hugo_report(report) if decision.should_generate else None
    return report, rendered, decision


def _already_published(client: WorkerAPIClient, options: Mapping[str, Any],
                       command_run_id: str, *, record_run: bool) -> Optional[Dict[str, Any]]:
    edition = ReportEdition(options["edition_value"])
    report_date = date.fromisoformat(options["date_value"]) if options.get("date_value") else datetime.now(ZoneInfo("Asia/Shanghai")).date()
    start, end = report_window(edition, report_date, from_value=options.get("from_value"), to_value=options.get("to_value"))
    period = "%d-w%02d" % report_date.isocalendar()[:2] if edition is ReportEdition.WEEKLY else report_date.isoformat()
    identity = "%s:%s:%s" % (edition.value, start.isoformat(), end.isoformat()) if edition is ReportEdition.AD_HOC else "%s:%s" % (edition.value, period)
    report_id = str(uuid5(REPORT_NAMESPACE, identity))
    existing = client.get_report(report_id).get("report")
    if not existing or existing.get("report_status") != "published":
        return None
    if record_run:
        client.update_run(command_run_id, {"run_status": "skipped", "item_count": 0,
                          "metadata": {"reason": "already_published", "report_id": report_id}},
                          idempotency_key="run:skip:%s" % command_run_id)
    return {"pipeline_run_id": command_run_id, "report_id": report_id, "status": "skipped",
            "reason": "already_published", "published_url": existing.get("published_url")}


def generate_report(
    repository: CatalogRepository,
    client: WorkerAPIClient,
    *,
    command_run_id: str,
    dry_run: bool,
    report_options: Mapping[str, Any],
) -> Dict[str, Any]:
    now = utc_now()
    if not dry_run:
        client.create_run(
            {
                "id": command_run_id,
                "run_type": "report_generate",
                "trigger_type": "multica",
                "run_status": "running",
                "started_at": now,
                "created_at": now,
                "metadata": {"edition": report_options["edition_value"]},
            },
            idempotency_key="run:create:%s" % command_run_id,
        )
    try:
        existing = _already_published(client, report_options, command_run_id, record_run=not dry_run)
        if existing:
            return existing
        report, rendered, decision = build_report(client, **report_options)
    except Exception as exc:
        if not dry_run:
            _fail_run(client, command_run_id, "report_input_failed", str(exc))
        raise
    if not decision.should_generate:
        if not dry_run:
            client.update_run(
                command_run_id,
                {
                    "run_status": "skipped",
                    "item_count": 0,
                    "metadata": {"reason": decision.reason},
                },
                idempotency_key="run:skip:%s" % command_run_id,
            )
        return {
            "pipeline_run_id": command_run_id,
            "status": "skipped",
            "reason": decision.reason,
        }
    assert rendered is not None
    root = repository_root(repository)
    if dry_run:
        return {
            "pipeline_run_id": command_run_id,
            "dry_run": True,
            "report_id": report.report_id,
            "path": rendered.relative_path.as_posix(),
            "signals": len(report.signals),
        }

    report_payload = _report_record(report, rendered.markdown)
    # A new generation attempt must execute its transitions again after a
    # failed validation, even when the report text has not changed. Transport
    # retries within this run still reuse the same idempotency keys.
    report_version = hashlib.sha256(
        (command_run_id + "\n" + rendered.markdown).encode("utf-8")
    ).hexdigest()[:16]
    try:
        client.create_report(
            report_payload,
            idempotency_key="report:create:%s:%s" % (report.report_id, report_version),
        )
        client.update_report_status(
            report.report_id,
            "validating",
            idempotency_key="report:validating:%s:%s" % (report.report_id, report_version),
        )
        output = root / rendered.relative_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered.markdown, encoding="utf-8")
        publication = PublicationService(
            PublishValidator.default(), GitPublisher(root)
        ).validate(report, rendered, changed_paths=(rendered.relative_path,))
        if publication.report.status is ReportStatus.FAILED:
            client.update_report_status(
                report.report_id,
                "failed",
                idempotency_key="report:failed:%s:%s" % (report.report_id, report_version),
            )
            client.update_run(
                command_run_id,
                {"run_status": "failed", "error_code": "publication_gate_failed"},
                idempotency_key="run:fail:%s" % command_run_id,
            )
            return {
                "pipeline_run_id": command_run_id,
                "report_id": report.report_id,
                "status": "failed",
                "gates": [_gate_dict(item) for item in publication.gates],
            }
        client.update_report_status(
            report.report_id,
            "ready",
            idempotency_key="report:ready:%s:%s" % (report.report_id, report_version),
        )
        client.update_run(
            command_run_id,
            {"run_status": "succeeded", "item_count": len(report.signals)},
            idempotency_key="run:finish:%s" % command_run_id,
        )
    except Exception as exc:
        try:
            client.update_report_status(
                report.report_id,
                "failed",
                idempotency_key="report:failed:%s:%s" % (report.report_id, report_version),
            )
        except Exception:
            pass
        _fail_run(client, command_run_id, "report_generation_failed", str(exc))
        raise
    return {
        "pipeline_run_id": command_run_id,
        "report_id": report.report_id,
        "status": "ready",
        "path": rendered.relative_path.as_posix(),
        "gates": [_gate_dict(item) for item in publication.gates],
    }


def publish_report(
    repository: CatalogRepository,
    client: WorkerAPIClient,
    *,
    command_run_id: str,
    execute: bool,
    push: bool,
    published_url: Optional[str],
    remote: str,
    branch: str,
    report_options: Mapping[str, Any],
) -> Dict[str, Any]:
    if push and not execute:
        raise CatalogError("--push requires --execute")
    if execute and push and not published_url:
        raise CatalogError("--published-url is required for an executed push")
    if execute:
        now = utc_now()
        client.create_run(
            {
                "id": command_run_id,
                "run_type": "report_publish",
                "trigger_type": "multica",
                "run_status": "running",
                "started_at": now,
                "created_at": now,
                "metadata": {"edition": report_options["edition_value"], "push": push},
            },
            idempotency_key="run:create:%s" % command_run_id,
        )
    try:
        existing = _already_published(client, report_options, command_run_id, record_run=execute)
        if existing:
            return existing
        report, rendered, decision = build_report(client, **report_options)
    except Exception as exc:
        if execute:
            _fail_run(client, command_run_id, "report_publish_input_failed", str(exc))
        raise
    try:
        return _publish_built_report(
            repository,
            client,
            command_run_id=command_run_id,
            execute=execute,
            push=push,
            published_url=published_url,
            remote=remote,
            branch=branch,
            report=report,
            rendered=rendered,
            decision=decision,
        )
    except Exception as exc:
        if execute:
            _fail_run(client, command_run_id, "report_publish_failed", str(exc))
        raise


def _publish_built_report(
    repository: CatalogRepository,
    client: WorkerAPIClient,
    *,
    command_run_id: str,
    execute: bool,
    push: bool,
    published_url: Optional[str],
    remote: str,
    branch: str,
    report: Report,
    rendered: Any,
    decision: Any,
) -> Dict[str, Any]:
    if not decision.should_generate or rendered is None:
        raise CatalogError("report has no publishable signals: %s" % decision.reason)
    root = repository_root(repository)
    output = root / rendered.relative_path
    if not output.exists() or output.read_text(encoding="utf-8") != rendered.markdown:
        raise CatalogError("generated report artifact is missing or differs; run report generate first")

    publication_service = PublicationService(PublishValidator.default(), GitPublisher(root))
    validated = publication_service.validate(
        report, rendered, changed_paths=(rendered.relative_path,)
    )
    if validated.report.status is not ReportStatus.READY:
        raise CatalogError("publication gates failed")
    result = publication_service.publish_ready(
        validated.report,
        rendered,
        published_url=published_url or "https://example.invalid/dry-run",
        push=push,
        dry_run=not execute,
        remote=remote,
        branch=branch,
    )
    git = result.git
    if execute and push and git and git.commit_sha:
        report_version = hashlib.sha256(rendered.markdown.encode("utf-8")).hexdigest()[:16]
        client.update_report_status(
            report.report_id,
            "published",
            published_url=published_url,
            git_commit=git.commit_sha,
            idempotency_key="report:published:%s:%s" % (report.report_id, report_version),
        )
    if execute:
        client.update_run(
            command_run_id,
            {
                "run_status": "succeeded",
                "item_count": len(report.signals),
                "metadata": {
                    "report_id": report.report_id,
                    "git_commit": git.commit_sha if git else None,
                    "pushed": bool(git and git.pushed),
                },
            },
            idempotency_key="run:finish:%s" % command_run_id,
        )
    return {
        "pipeline_run_id": command_run_id,
        "report_id": report.report_id,
        "status": "published" if execute and push else "ready",
        "dry_run": not execute,
        "pushed": bool(git and git.pushed),
        "commit_sha": git.commit_sha if git else None,
        "commands": [list(command) for command in (git.commands if git else ())],
    }


def scheduler_plan(repository: CatalogRepository) -> Dict[str, Any]:
    root = repository_root(repository)
    schedules_path = root / "intelligence" / "config" / "schedules.yaml"
    schedules = yaml.safe_load(schedules_path.read_text(encoding="utf-8"))
    if not isinstance(schedules, Mapping) or not isinstance(schedules.get("jobs"), list):
        raise CatalogError("schedules.yaml must contain a jobs array")
    launchd_jobs = [
        job
        for job in schedules["jobs"]
        if isinstance(job, Mapping) and job.get("owner") == "launchd" and job.get("enabled")
    ]
    if len(launchd_jobs) != 1 or launchd_jobs[0].get("id") != "collect-due":
        raise CatalogError("v1 expects exactly one enabled launchd collect-due job")
    minutes = int(launchd_jobs[0].get("cadence", {}).get("minutes", 0))
    if minutes < 5:
        raise CatalogError("collect-due interval must be at least 5 minutes")
    template_path = root / "intelligence" / "launchd" / "com.fatflowers.personal-intelligence.collect.plist.template"
    rendered = template_path.read_text(encoding="utf-8").replace(
        "__REPOSITORY_PATH__", str(root)
    ).replace("<integer>1800</integer>", "<integer>%d</integer>" % (minutes * 60), 1)
    try:
        plistlib.loads(rendered.encode("utf-8"))
    except Exception as exc:
        raise CatalogError("rendered launchd plist is invalid: %s" % exc) from exc
    destination = Path(
        os.getenv(
            "INTELLIGENCE_LAUNCH_AGENTS_DIR",
            str(Path.home() / "Library" / "LaunchAgents"),
        )
    ) / "com.fatflowers.personal-intelligence.collect.plist"
    return {
        "jobs": launchd_jobs,
        "destination": str(destination),
        "rendered_plist": rendered,
        "commands": [
            ["plutil", "-lint", str(destination)],
            ["launchctl", "bootstrap", "gui/<uid>", str(destination)],
            ["launchctl", "enable", "gui/<uid>/com.fatflowers.personal-intelligence.collect"],
        ],
    }


def scheduler_apply(repository: CatalogRepository, *, dry_run: bool) -> Dict[str, Any]:
    plan = scheduler_plan(repository)
    if dry_run:
        return {**plan, "dry_run": True}
    destination = Path(plan["destination"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(plan["rendered_plist"], encoding="utf-8")
    lint = subprocess.run(
        ("plutil", "-lint", str(destination)), capture_output=True, text=True, check=False
    )
    if lint.returncode:
        raise CatalogError((lint.stderr or lint.stdout or "plutil failed").strip())
    uid = os.getuid()
    service = "gui/%d/com.fatflowers.personal-intelligence.collect" % uid
    domain = "gui/%d" % uid
    current = subprocess.run(
        ("launchctl", "print", service), capture_output=True, text=True, check=False
    )
    if current.returncode == 0:
        stopped = subprocess.run(
            ("launchctl", "bootout", domain, str(destination)),
            capture_output=True,
            text=True,
            check=False,
        )
        if stopped.returncode:
            raise CatalogError((stopped.stderr or stopped.stdout or "launchctl bootout failed").strip())
    loaded = subprocess.run(
        ("launchctl", "bootstrap", domain, str(destination)),
        capture_output=True,
        text=True,
        check=False,
    )
    if loaded.returncode:
        raise CatalogError((loaded.stderr or loaded.stdout or "launchctl bootstrap failed").strip())
    enabled = subprocess.run(
        ("launchctl", "enable", service), capture_output=True, text=True, check=False
    )
    if enabled.returncode:
        raise CatalogError((enabled.stderr or enabled.stdout or "launchctl enable failed").strip())
    return {
        "dry_run": False,
        "installed": str(destination),
        "loaded": True,
        "service": service,
    }


def _report_signal(row: Mapping[str, Any]) -> ReportSignal:
    analysis = validate_analysis(
        {
            "headline": row.get("headline"),
            "summary": row.get("summary"),
            "key_change": row.get("key_change"),
            "why_it_matters": row.get("why_it_matters"),
            "company_impact": row.get("company_impact"),
            "importance": row.get("importance"),
            "confidence": row.get("confidence"),
            "topics": _json_array(row.get("topics_json")),
            "watch_next": _json_array(row.get("watch_next_json")),
            "evidence": _json_array(row.get("evidence_json")),
        }
    )
    original_url = str(row.get("canonical_url") or row.get("url") or "")
    sources = (ReportSource(original_url, "原文"),) + tuple(
        ReportSource(evidence.url, str(row.get("title") or evidence.claim))
        for evidence in analysis.evidence
        if evidence.url != original_url
    )
    return ReportSignal(
        item_id=str(row["id"]),
        target=str(row.get("target_name") or row.get("target_slug") or "Unknown"),
        title=str(row.get("title") or row.get("url") or "Untitled"),
        published_at=_datetime(str(row["published_at"])),
        analysis=analysis,
        sources=sources,
        date_kind="observed_change" if verified_observed_change(_json_object(row.get("raw_metadata_json", row.get("raw_metadata")))) else "published",
        source_label="%s / %s" % (
            str(row.get("target_name") or row.get("target_slug") or "Unknown"),
            str(row.get("channel_name") or row.get("channel_slug") or "Unknown"),
        ),
    )


def _report_record(report: Report, markdown: str) -> Dict[str, Any]:
    return {
        "id": report.report_id,
        "report_date": report.generated_at.date().isoformat(),
        "edition": report.edition.value,
        "window_start": report.window_start.isoformat(),
        "window_end": report.window_end.isoformat(),
        "title": report.title,
        "slug": "%s-%s" % (report.period, report.edition.value),
        "content_markdown": markdown,
        "created_at": report.generated_at.isoformat(),
        "items": [
            {"item_id": signal.item_id, "rank": rank, "section": "key-signals"}
            for rank, signal in enumerate(report.signals, 1)
        ],
    }


def _gate_dict(value: Any) -> Dict[str, Any]:
    return {"name": value.name, "passed": value.passed, "message": value.message}


def _json_object(value: Any) -> Dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise CatalogError("stored report input contains invalid JSON") from exc
    if not isinstance(parsed, list):
        raise CatalogError("stored report input JSON must be an array")
    return parsed


def _datetime(value: str) -> datetime:
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise CatalogError("invalid ISO timestamp: %s" % value) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _fail_run(client: WorkerAPIClient, run_id: str, code: str, summary: str) -> None:
    try:
        client.update_run(
            run_id,
            {
                "run_status": "failed",
                "error_code": code,
                "error_summary": summary[:2000],
            },
            idempotency_key="run:fail:%s" % run_id,
        )
    except Exception:
        pass
