"""Deterministic CLI consumed by humans and the Multica operator."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from intelligence.catalog import (
    CatalogError,
    CatalogRepository,
    CatalogService,
    CatalogValidationError,
)
from intelligence.observability import emit_event, new_run_id
from intelligence.storage import StorageClientError, WorkerAPIClient
from .research import research_plan, research_hydrate, research_ingest, research_coverage, research_run, research_discover, resolve_mcp_fallbacks

from .operations import (
    collection_plan,
    collect_local,
    generate_report,
    ingest_analyses,
    ingest_collection,
    list_mcp_bindings,
    load_json_input,
    pending_analysis,
    publish_report,
    scheduler_apply,
    scheduler_plan,
    show_mcp_binding,
    verify_mcp_binding,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="intelctl")
    parser.add_argument("--catalog", type=Path, help="path to catalog.yaml")
    parser.add_argument("--schema", type=Path, help="path to catalog JSON Schema")
    parser.add_argument("--api-url", help="override INTELLIGENCE_API_URL")
    parser.add_argument("--dry-run", action="store_true", help="validate changes without writing")
    commands = parser.add_subparsers(dest="command", required=True)

    target = commands.add_parser("target")
    target_commands = target.add_subparsers(dest="target_command", required=True)
    target_commands.add_parser("list")
    target_show = target_commands.add_parser("show")
    target_show.add_argument("slug")
    target_add = target_commands.add_parser("add")
    target_add.add_argument("slug")
    target_add.add_argument("--name", required=True)
    target_add.add_argument("--type", required=True, dest="target_type")
    target_add.add_argument("--description")
    target_add.add_argument("--priority", choices=_priorities(), default="normal")
    target_add.add_argument("--enabled", action="store_true")
    target_update = target_commands.add_parser("update")
    target_update.add_argument("slug")
    target_update.add_argument("--name")
    target_update.add_argument("--type", dest="target_type")
    target_update.add_argument("--description")
    target_update.add_argument("--priority", choices=_priorities())
    target_update.add_argument("--enabled", action="store_true", default=None)
    target_disable = target_commands.add_parser("disable")
    target_disable.add_argument("slug")
    target_enable = target_commands.add_parser("enable")
    target_enable.add_argument("slug")

    channel = commands.add_parser("channel")
    channel_commands = channel.add_subparsers(dest="channel_command", required=True)
    channel_list = channel_commands.add_parser("list")
    channel_list.add_argument("--target")
    channel_add = channel_commands.add_parser("add")
    channel_add.add_argument("slug")
    channel_add.add_argument("--target", required=True)
    channel_add.add_argument("--name", required=True)
    channel_add.add_argument("--type", required=True, dest="channel_type")
    channel_add.add_argument(
        "--collector", choices=["mcp", "rss", "http", "browser", "github_api", "mcp_registry_api"], required=True
    )
    channel_add.add_argument("--url")
    channel_add.add_argument("--handle")
    channel_add.add_argument("--interval", type=int, default=60, dest="interval_minutes")
    channel_add.add_argument("--priority", choices=_priorities(), default="normal")
    channel_add.add_argument("--tool-binding")
    channel_add.add_argument("--enabled", action="store_true")
    channel_update = channel_commands.add_parser("update")
    channel_update.add_argument("slug")
    channel_update.add_argument("--name")
    channel_update.add_argument("--interval", type=int, dest="interval_minutes")
    channel_update.add_argument("--priority", choices=_priorities())
    channel_update.add_argument("--url")
    channel_update.add_argument("--handle")
    channel_bind = channel_commands.add_parser("bind-tool")
    channel_bind.add_argument("slug")
    channel_bind.add_argument("binding")
    channel_interval = channel_commands.add_parser("set-interval")
    channel_interval.add_argument("slug")
    channel_interval.add_argument("minutes", type=int)
    channel_disable = channel_commands.add_parser("disable")
    channel_disable.add_argument("slug")
    channel_enable = channel_commands.add_parser("enable")
    channel_enable.add_argument("slug")
    channel_test = channel_commands.add_parser("test")
    channel_test.add_argument("slug")

    tag = commands.add_parser("tag")
    tag_commands = tag.add_subparsers(dest="tag_command", required=True)
    tag_commands.add_parser("list")
    tag_add = tag_commands.add_parser("add")
    tag_add.add_argument("slug")
    tag_add.add_argument("--name", required=True)
    tag_add.add_argument("--type", required=True, dest="tag_type")
    for action in ("attach", "detach"):
        tag_relation = tag_commands.add_parser(action)
        tag_relation.add_argument("slug")
        destination = tag_relation.add_mutually_exclusive_group(required=True)
        destination.add_argument("--target")
        destination.add_argument("--channel")

    catalog = commands.add_parser("catalog")
    catalog_commands = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_commands.add_parser("validate")
    catalog_commands.add_parser("sync")

    commands.add_parser("status")
    run = commands.add_parser("run")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    run_show = run_commands.add_parser("show")
    run_show.add_argument("run_id")
    run_list = run_commands.add_parser("list")
    run_list.add_argument("--status", choices=["pending", "running", "succeeded", "failed", "skipped"])
    run_list.add_argument("--limit", type=int, default=20)

    collect = commands.add_parser("collect")
    # Compatibility with the launchd template and reviewed runbooks.
    collect.add_argument("--due", action="store_true", dest="legacy_due")
    collect.add_argument("--target", dest="legacy_target")
    collect.add_argument("--channel", dest="legacy_channel")
    collect_commands = collect.add_subparsers(dest="collect_command")
    collect_plan_parser = collect_commands.add_parser("plan")
    collect_selection = collect_plan_parser.add_mutually_exclusive_group(required=True)
    collect_selection.add_argument("--due", action="store_true")
    collect_selection.add_argument("--target")
    collect_selection.add_argument("--channel")
    collect_plan_parser.add_argument("--limit", type=int, default=100)
    collect_ingest = collect_commands.add_parser("ingest")
    collect_ingest.add_argument("--channel", required=True)
    collect_ingest.add_argument("--input", type=Path, help="JSON file; omit or use - for stdin")
    collect_ingest.add_argument("--run-id", help="existing pipeline run to finish")
    collect_local_parser = collect_commands.add_parser("local")
    collect_local_selection = collect_local_parser.add_mutually_exclusive_group(required=True)
    collect_local_selection.add_argument("--due", action="store_true")
    collect_local_selection.add_argument("--channel")
    collect_local_parser.add_argument("--run-id", help="existing pipeline run to finish")
    collect_local_parser.add_argument("--limit", type=int, default=100)

    research = commands.add_parser("research")
    research_commands = research.add_subparsers(dest="research_command", required=True)
    for action in ("discover", "plan", "run", "hydrate", "ingest", "coverage"):
        command = research_commands.add_parser(action)
        command.add_argument("--since", help="ISO publication cutoff; default last 72 hours")
        if action in ("run", "hydrate", "discover"):
            command.add_argument("--mcp", action="store_true", help="execute fixed Firecrawl fallbacks through authenticated Codex MCP capture")
        if action in ("plan", "run"):
            command.add_argument("--limit", type=int, default=30)
            command.add_argument("--target")
        if action == "discover":
            command.add_argument("--target")
        if action in ("hydrate", "ingest"):
            command.add_argument("--item-id", required=True)
        if action == "ingest":
            command.add_argument("--input", type=Path)

    analyze = commands.add_parser("analyze")
    analyze.add_argument("--pending", action="store_true", dest="legacy_pending")
    analyze.add_argument("--limit", type=int, default=100, dest="legacy_limit")
    analyze_commands = analyze.add_subparsers(dest="analyze_command")
    analyze_pending = analyze_commands.add_parser("pending")
    analyze_pending.add_argument("--limit", type=int, default=100)
    analyze_pending.add_argument("--target")
    analyze_pending.add_argument("--channel")
    analyze_pending.add_argument("--since", help="ISO publication cutoff; default last 72 hours")
    analyze_ingest = analyze_commands.add_parser("ingest")
    analyze_ingest.add_argument("--input", type=Path, help="JSON file; omit or use - for stdin")
    analyze_ingest.add_argument("--run-id", help="pipeline run returned by analyze pending")
    analyze_ingest.add_argument("--model", default="codex")
    analyze_ingest.add_argument("--prompt-version", default="v1")

    report = commands.add_parser("report")
    report_commands = report.add_subparsers(dest="report_command", required=True)
    report_reconcile = report_commands.add_parser("reconcile")
    report_reconcile.add_argument("--report-id", required=True)
    report_reconcile.add_argument("--input", type=Path, required=True)
    report_reconcile.add_argument("--git-commit", required=True)
    report_generate = report_commands.add_parser("generate")
    _add_report_arguments(report_generate)
    report_generate.add_argument("--dry-run", action="store_true")
    report_publish = report_commands.add_parser("publish")
    _add_report_arguments(report_publish)
    report_publish.add_argument("--execute", action="store_true", help="allow Git commit")
    report_publish.add_argument("--push", action="store_true", help="allow Git push")
    report_publish.add_argument("--published-url")
    report_publish.add_argument("--remote", default="origin")
    report_publish.add_argument("--branch", default="main")
    report_revise = report_commands.add_parser("revise")
    report_revise.add_argument("--report-id", required=True)
    report_revise.add_argument("--input", type=Path, required=True)
    report_revise.add_argument("--title", required=True)
    report_revise.add_argument("--reason", required=True)
    report_revise.add_argument("--git-commit", required=True)
    report_revise.add_argument("--expected-git-commit", required=True)
    report_revise.add_argument("--item-id", action="append", dest="item_ids")

    scheduler = commands.add_parser("scheduler")
    scheduler_commands = scheduler.add_subparsers(dest="scheduler_command", required=True)
    scheduler_commands.add_parser("plan")
    scheduler_apply_parser = scheduler_commands.add_parser("apply")
    scheduler_apply_parser.add_argument("--dry-run", action="store_true")

    audit = commands.add_parser("audit")
    audit_commands = audit.add_subparsers(dest="audit_command", required=True)
    audit_list = audit_commands.add_parser("list")
    audit_list.add_argument("--entity-type")
    audit_list.add_argument("--entity-id")
    audit_list.add_argument("--limit", type=int, default=100)
    audit_create = audit_commands.add_parser("create")
    audit_create.add_argument("--actor", default="intelctl")
    audit_create.add_argument("--action", required=True)
    audit_create.add_argument("--entity-type", required=True)
    audit_create.add_argument("--entity-id", required=True)
    audit_create.add_argument("--before", type=Path, help="JSON file")
    audit_create.add_argument("--after", type=Path, help="JSON file")

    mcp = commands.add_parser("mcp")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    binding = mcp_commands.add_parser("binding")
    binding_commands = binding.add_subparsers(dest="binding_command", required=True)
    binding_commands.add_parser("list")
    binding_show = binding_commands.add_parser("show")
    binding_show.add_argument("alias")
    binding_verify = binding_commands.add_parser("verify")
    binding_verify.add_argument("alias")
    binding_verify.add_argument("--evidence", required=True)
    binding_verify.add_argument("--dry-run", action="store_true")
    return parser


def _priorities() -> List[str]:
    return ["low", "normal", "high", "critical"]


def _add_report_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--edition", choices=["morning", "midday", "evening", "weekly", "ad-hoc"], required=True
    )
    parser.add_argument("--date", help="report date in YYYY-MM-DD")
    parser.add_argument("--from", dest="from_value", help="explicit ISO window start")
    parser.add_argument("--to", dest="to_value", help="explicit ISO window end")
    parser.add_argument("--title")
    parser.add_argument("--description")
    parser.add_argument("--trend", action="append", default=[])
    parser.add_argument("--tag")
    parser.add_argument("--target")


def execute(args: argparse.Namespace) -> Any:
    repository = CatalogRepository(args.catalog, args.schema)
    service = CatalogService(repository)
    client = WorkerAPIClient(args.api_url)

    if args.command == "target":
        if args.target_command == "list":
            return service.list_targets()
        if args.target_command == "show":
            return service.show_target(args.slug)
        if args.target_command == "add":
            return service.add_target(
                args.slug,
                args.name,
                args.target_type,
                args.description,
                args.priority,
                args.enabled,
                args.dry_run,
            )
        if args.target_command == "update":
            return service.update_target(
                args.slug,
                {
                    "name": args.name,
                    "type": args.target_type,
                    "description": args.description,
                    "priority": args.priority,
                    "enabled": args.enabled,
                },
                args.dry_run,
            )
        if args.target_command == "disable":
            return service.disable_target(args.slug, args.dry_run)
        return service.update_target(args.slug, {"enabled": True}, args.dry_run)

    if args.command == "channel":
        if args.channel_command == "list":
            return service.list_channels(args.target)
        if args.channel_command == "add":
            return service.add_channel(
                args.target,
                args.slug,
                args.name,
                args.channel_type,
                args.collector,
                args.url,
                args.handle,
                args.interval_minutes,
                args.priority,
                args.tool_binding,
                args.enabled,
                dry_run=args.dry_run,
            )
        if args.channel_command == "update":
            return service.update_channel(
                args.slug,
                {
                    "name": args.name,
                    "interval_minutes": args.interval_minutes,
                    "priority": args.priority,
                    "url": args.url,
                    "handle": args.handle,
                },
                args.dry_run,
            )
        if args.channel_command == "bind-tool":
            return service.update_channel(
                args.slug, {"tool_binding": args.binding}, args.dry_run
            )
        if args.channel_command == "set-interval":
            return service.update_channel(
                args.slug, {"interval_minutes": args.minutes}, args.dry_run
            )
        if args.channel_command == "disable":
            return service.disable_channel(args.slug, args.dry_run)
        if args.channel_command == "enable":
            return service.update_channel(args.slug, {"enabled": True}, args.dry_run)
        channel = next(
            (item for item in service.list_channels() if item["slug"] == args.slug),
            None,
        )
        if channel is None:
            raise CatalogError("channel not found: %s" % args.slug)
        return {
            "channel": args.slug,
            "configuration_valid": True,
            "live_call_performed": False,
            "note": "collector-specific live test is delegated to the collector adapter",
        }

    if args.command == "tag":
        if args.tag_command == "list":
            return service.list_tags()
        if args.tag_command == "add":
            return service.add_tag(
                args.slug, args.name, args.tag_type, args.dry_run
            )
        operation = service.attach_tag if args.tag_command == "attach" else service.detach_tag
        return operation(
            args.slug,
            target_slug=args.target,
            channel_slug=args.channel,
            dry_run=args.dry_run,
        )

    if args.command == "catalog":
        catalog = repository.load()
        if args.catalog_command == "validate":
            return {
                "valid": True,
                "path": str(repository.path),
                "targets": len(catalog.targets),
                "channels": sum(len(target.channels) for target in catalog.targets),
                "tags": len(catalog.tags),
            }
        payload = catalog.to_sync_dict()
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if args.dry_run:
            return {"dry_run": True, "catalog_sha256": digest, "payload": payload}
        response = client.sync_catalog(payload, "catalog:%s" % digest)
        client.create_audit_event(
            {
                "id": args.execution_run_id,
                "actor": "intelctl",
                "action": "catalog.sync",
                "entity_type": "catalog",
                "entity_id": "catalog",
                "after": {"sha256": digest},
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            idempotency_key="audit:%s" % args.execution_run_id,
        )
        return response

    if args.command == "status":
        catalog = repository.load()
        local = {
            "catalog_valid": True,
            "targets": len(catalog.targets),
            "enabled_targets": sum(target.enabled for target in catalog.targets),
            "channels": sum(len(target.channels) for target in catalog.targets),
            "enabled_channels": sum(
                channel.enabled for target in catalog.targets for channel in target.channels
            ),
        }
        remote: Dict[str, Any] = {"configured": client.configured}
        if client.configured:
            remote.update(client.health())
        return {"local": local, "remote": remote}

    if args.command == "run":
        if args.run_command == "show":
            return client.get_run(args.run_id)
        return client.list_runs(args.status, args.limit)

    if args.command == "collect":
        if args.collect_command == "ingest":
            return ingest_collection(
                repository,
                client,
                channel_slug=args.channel,
                payload=load_json_input(args.input),
                command_run_id=args.execution_run_id,
                external_run_id=args.run_id,
            )
        if args.collect_command == "local":
            return collect_local(
                repository,
                client,
                command_run_id=args.execution_run_id,
                due=args.due,
                channel_slug=args.channel,
                external_run_id=args.run_id,
                limit=args.limit,
            )
        due = args.due if args.collect_command == "plan" else args.legacy_due
        target_slug = args.target if args.collect_command == "plan" else args.legacy_target
        channel_slug = args.channel if args.collect_command == "plan" else args.legacy_channel
        limit = args.limit if args.collect_command == "plan" else 100
        return collection_plan(
            repository,
            client,
            due=due,
            target_slug=target_slug,
            channel_slug=channel_slug,
            limit=limit,
            command_run_id=args.execution_run_id,
        )

    if args.command == "research":
        if args.research_command == "discover":
            result = research_discover(repository, client, target=args.target)
            return resolve_mcp_fallbacks(client, result, since=args.since) if args.mcp else result
        if args.research_command == "run":
            result = research_run(client, since=args.since, limit=args.limit, target=args.target)
            return resolve_mcp_fallbacks(client, result, since=args.since) if args.mcp else result
        if args.research_command == "plan":
            return research_plan(client, since=args.since, limit=args.limit, target=args.target)
        if args.research_command == "coverage":
            return research_coverage(client, since=args.since)
        if args.research_command == "hydrate":
            result = research_hydrate(client, item_id=args.item_id, since=args.since)
            return resolve_mcp_fallbacks(client, result, since=args.since) if args.mcp else result
        return research_ingest(client, item_id=args.item_id, since=args.since,
                               payload=load_json_input(args.input))

    if args.command == "analyze":
        if args.analyze_command == "pending" or args.legacy_pending:
            limit = args.limit if args.analyze_command == "pending" else args.legacy_limit
            target = args.target if args.analyze_command == "pending" else None
            channel = args.channel if args.analyze_command == "pending" else None
            return pending_analysis(
                client,
                command_run_id=args.execution_run_id,
                limit=limit,
                target_slug=target,
                channel_slug=channel,
                since=args.since if args.analyze_command == "pending" else None,
            )
        if args.analyze_command is None:
            raise CatalogError("use analyze pending, analyze --pending, or analyze ingest")
        return ingest_analyses(
            client,
            payload=load_json_input(args.input),
            command_run_id=args.execution_run_id,
            external_run_id=args.run_id,
            model=args.model,
            prompt_version=args.prompt_version,
        )

    if args.command == "report":
        if args.report_command == "reconcile":
            from intelligence.cli.operations import reconcile_report
            return reconcile_report(repository, client, args.report_id, args.input, args.git_commit, args.execution_run_id)
        if args.report_command == "revise":
            return client.revise_published_report(
                args.report_id,
                title=args.title,
                content_markdown=args.input.read_text(encoding="utf-8"),
                reason=args.reason,
                git_commit=args.git_commit,
                expected_git_commit=args.expected_git_commit,
                item_ids=args.item_ids,
                idempotency_key="report-revision:%s:%s"
                % (args.report_id, args.git_commit),
            )
        options = {
            "edition_value": args.edition,
            "date_value": args.date,
            "from_value": args.from_value,
            "to_value": args.to_value,
            "title": args.title,
            "description": args.description,
            "trends": args.trend,
            "tag": args.tag,
            "target_slug": args.target,
        }
        if args.report_command == "generate":
            return generate_report(
                repository,
                client,
                command_run_id=args.execution_run_id,
                dry_run=args.dry_run,
                report_options=options,
            )
        return publish_report(
            repository,
            client,
            command_run_id=args.execution_run_id,
            execute=args.execute,
            push=args.push,
            published_url=args.published_url,
            remote=args.remote,
            branch=args.branch,
            report_options=options,
        )

    if args.command == "scheduler":
        if args.scheduler_command == "plan":
            return scheduler_plan(repository)
        if not args.dry_run and (not client.configured or not client.token):
            raise StorageClientError(
                "scheduler apply requires Worker API configuration so the change can be audited"
            )
        result = scheduler_apply(repository, dry_run=args.dry_run)
        if not args.dry_run:
            client.create_audit_event(
                {
                    "id": args.execution_run_id,
                    "actor": "intelctl",
                    "action": "scheduler.apply",
                    "entity_type": "scheduler",
                    "entity_id": "collect-due",
                    "after": {"installed": result.get("installed")},
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                idempotency_key="audit:%s" % args.execution_run_id,
            )
        return result

    if args.command == "audit":
        if args.audit_command == "list":
            return client.list_audit_events(
                entity_type=args.entity_type,
                entity_id=args.entity_id,
                limit=args.limit,
            )
        before = load_json_input(args.before) if args.before else None
        after = load_json_input(args.after) if args.after else None
        event = {
            "id": args.execution_run_id,
            "actor": args.actor,
            "action": args.action,
            "entity_type": args.entity_type,
            "entity_id": args.entity_id,
            "before": before,
            "after": after,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return client.create_audit_event(
            event, idempotency_key="audit:%s" % args.execution_run_id
        )

    if args.command == "mcp":
        if args.binding_command == "list":
            return list_mcp_bindings(repository)
        if args.binding_command == "show":
            return show_mcp_binding(repository, args.alias)
        return verify_mcp_binding(
            repository, args.alias, args.evidence, dry_run=args.dry_run
        )

    raise CatalogError("unsupported command")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    run_id = new_run_id()
    args.execution_run_id = run_id
    emit_event(run_id, "command.started", command=args.command)
    try:
        data = execute(args)
    except (CatalogValidationError, CatalogError, StorageClientError, ValueError) as exc:
        errors = exc.errors if isinstance(exc, CatalogValidationError) else [str(exc)]
        emit_event(run_id, "command.failed", level="error", errors=errors)
        print(
            json.dumps(
                {"ok": False, "run_id": run_id, "errors": errors},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    except Exception as exc:  # defensive CLI boundary; details stay out of stdout
        emit_event(run_id, "command.failed", level="error", error_type=type(exc).__name__)
        print(
            json.dumps(
                {
                    "ok": False,
                    "run_id": run_id,
                    "errors": ["unexpected internal error"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1

    emit_event(run_id, "command.succeeded", command=args.command)
    print(
        json.dumps(
            {"ok": True, "run_id": run_id, "data": data},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
