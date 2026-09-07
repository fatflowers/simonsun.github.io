import json
from pathlib import Path

import yaml

from intelligence.catalog import CatalogRepository
from intelligence.cli.operations import (
    build_report,
    collection_plan,
    collect_local,
    ingest_analyses,
    ingest_collection,
    report_window,
    scheduler_apply,
)
from intelligence.collectors import CollectionPage
from intelligence.normalize import NormalizedItem
from intelligence.reporter import ReportEdition

from test_catalog import SCHEMA, sample_catalog


class FakeClient:
    def __init__(self):
        self.calls = []
        self.report_items = []
        self.due_channels = []
        self.remote_channels = []

    def create_run(self, payload, *, idempotency_key):
        self.calls.append(("create_run", payload, idempotency_key))
        return {"created": True}

    def update_run(self, run_id, payload, *, idempotency_key):
        self.calls.append(("update_run", run_id, payload, idempotency_key))
        return {"run_status": payload["run_status"]}

    def get_due_channels(self, **kwargs):
        self.calls.append(("due", kwargs))
        return {"channels": self.due_channels}

    def get_catalog(self):
        return {"channels": self.remote_channels}

    def write_items(self, items, *, idempotency_key, channel_state=None):
        self.calls.append(("items", items, channel_state, idempotency_key))
        return {"inserted": len(items), "duplicates": 0}

    def get_pending_analysis(self, **kwargs):
        return {"items": []}

    def write_analyses(self, values, *, idempotency_key):
        self.calls.append(("analyses", values, idempotency_key))
        return {"upserted": len(values)}

    def get_report_input(self, **kwargs):
        self.calls.append(("report_input", kwargs))
        return {"items": self.report_items}

    def get_report(self, report_id):
        return {"report": None}


def project(tmp_path: Path):
    root = tmp_path / "site"
    (root / "intelligence" / "config").mkdir(parents=True)
    (root / "intelligence" / "launchd").mkdir(parents=True)
    (root / "hugo.toml").write_text('baseURL = "/"\n', encoding="utf-8")
    catalog = sample_catalog()
    catalog_path = root / "intelligence" / "config" / "catalog.yaml"
    catalog_path.write_text(yaml.safe_dump(catalog, allow_unicode=True), encoding="utf-8")
    registry = {
        "version": 1,
        "server": {"name": "aisa-tools", "url": "https://tools.aisa.one/mcp"},
        "tools": {
            "firecrawl-page-scrape-v1": {
                "status": "verified",
                "tool_name": "post_firecrawl_scrape",
                "channel_types": ["blog"],
                "input_template": {"url": "{{ channel.url }}"},
                "output_adapter": "firecrawl_document_v1",
            }
        },
    }
    (root / "intelligence" / "config" / "mcp-tools.yaml").write_text(
        yaml.safe_dump(registry), encoding="utf-8"
    )
    schedules = {
        "jobs": [
            {
                "id": "collect-due",
                "owner": "launchd",
                "enabled": True,
                "cadence": {"type": "interval", "minutes": 30},
            }
        ]
    }
    (root / "intelligence" / "config" / "schedules.yaml").write_text(
        yaml.safe_dump(schedules), encoding="utf-8"
    )
    template = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>Label</key><string>x</string>
<key>WorkingDirectory</key><string>__REPOSITORY_PATH__</string>
<key>StartInterval</key><integer>1800</integer></dict></plist>"""
    (root / "intelligence" / "launchd" / "com.fatflowers.personal-intelligence.collect.plist.template").write_text(
        template, encoding="utf-8"
    )
    return root, CatalogRepository(catalog_path, SCHEMA)


def test_manual_collection_plan_renders_fixed_tool_arguments(tmp_path):
    _, repository = project(tmp_path)
    result = collection_plan(
        repository,
        FakeClient(),
        due=False,
        target_slug=None,
        channel_slug="composio-blog",
        limit=10,
    )

    assert result["plans"][0]["tool_name"] == "post_firecrawl_scrape"
    assert result["plans"][0]["arguments"] == {"url": "https://composio.dev/blog"}


def test_collection_ingest_normalizes_and_tracks_run(tmp_path):
    _, repository = project(tmp_path)
    client = FakeClient()
    result = ingest_collection(
        repository,
        client,
        channel_slug="composio-blog",
        payload={
            "data": {
                "markdown": "A public update",
                "metadata": {"sourceURL": "https://composio.dev/blog/update", "title": "Update"},
            }
        },
        command_run_id="run-1",
    )

    assert result["normalized"] == 1
    item_call = next(call for call in client.calls if call[0] == "items")
    assert item_call[1][0]["content_hash"]
    assert item_call[2]["succeeded"] is True
    assert client.calls[-1][2]["run_status"] == "succeeded"


def test_mcp_diff_baseline_unchanged_and_distinct_observed_changes(tmp_path):
    root, repository = project(tmp_path)
    path = root / "intelligence/config/catalog.yaml"
    catalog = yaml.safe_load(path.read_text())
    catalog["targets"][0]["channels"][0]["config"] = {"diff": True}
    path.write_text(yaml.safe_dump(catalog))
    client = FakeClient()
    def collect(body, run):
        result = ingest_collection(repository, client, channel_slug="composio-blog",
            payload={"data": {"markdown": body, "metadata": {"sourceURL": "https://composio.dev/pricing", "title": "Pricing"}}},
            command_run_id=run)
        call = [call for call in client.calls if call[0] == "items"][-1]
        client.remote_channels = [{"slug": "composio-blog", "cursor_json": json.dumps(call[2]["cursor"])}]
        return result, call[1]
    assert collect("The old pricing includes twenty tools and costs $20 monthly.", "baseline")[0]["normalized"] == 0
    assert collect("The old pricing includes twenty tools and costs $20 monthly.", "unchanged")[0]["normalized"] == 0
    _, first = collect("New pricing includes forty tools and costs $10 monthly.", "change1")
    _, second = collect("New pricing includes sixty tools and costs $5 monthly.", "change2")
    assert len(first) == len(second) == 1
    assert first[0]["id"] != second[0]["id"]
    assert first[0]["external_id"] != second[0]["external_id"]
    assert first[0]["published_at"] == first[0]["raw_metadata"]["web_diff"]["observed_at"]
    assert first[0]["raw_metadata"]["date_kind"] == "observed_change"
    assert "$20" in first[0]["raw_metadata"]["web_diff"]["before_text"]


class LocalSuccess:
    def collect(self, channel, cursor=None):
        return CollectionPage.of(
            [
                NormalizedItem(
                    external_id="entry-1",
                    target_slug=channel.target_slug,
                    channel_slug=channel.channel_slug,
                    url="https://composio.dev/blog/entry-1",
                    title="Entry",
                    author=None,
                    published_at="2026-09-06T00:00:00Z",
                    content_text="Update",
                )
            ],
            next_cursor={"last_external_id": "entry-1"},
            raw_count=1,
        )


class LocalFailure:
    def collect(self, channel, cursor=None):
        raise RuntimeError("local source unavailable")


def test_local_collect_executes_declared_fallback_and_commits_cursor(tmp_path):
    _, repository = project(tmp_path)
    client = FakeClient()
    result = collect_local(
        repository,
        client,
        command_run_id="local-1",
        due=False,
        channel_slug="composio-blog",
        collectors={"http": LocalSuccess()},
    )

    assert result["status"] == "succeeded"
    assert result["channels"][0]["collector"] == "http"
    item_call = next(call for call in client.calls if call[0] == "items")
    assert item_call[2]["cursor"] == {"last_external_id": "entry-1"}
    assert item_call[2]["succeeded"] is True


def test_local_collect_failure_updates_channel_health_and_run(tmp_path):
    _, repository = project(tmp_path)
    client = FakeClient()
    result = collect_local(
        repository,
        client,
        command_run_id="local-2",
        due=False,
        channel_slug="composio-blog",
        collectors={"http": LocalFailure()},
    )

    assert result["status"] == "failed"
    health_call = next(call for call in client.calls if call[0] == "items")
    assert health_call[2]["succeeded"] is False
    assert client.calls[-1][2]["run_status"] == "failed"


def test_local_collect_due_filters_mcp_and_executes_rss(tmp_path):
    _, repository = project(tmp_path)
    value = repository.load_raw()
    value["targets"][0]["channels"].append(
        {
            "slug": "composio-feed",
            "name": "Feed",
            "type": "rss",
            "collector": "rss",
            "url": "https://composio.dev/feed.xml",
            "interval_minutes": 60,
            "enabled": True,
            "tags": ["official"],
        }
    )
    repository.save(value)
    client = FakeClient()
    client.due_channels = [
        {"slug": "composio-blog", "collector_type": "mcp"},
        {"slug": "composio-feed", "collector_type": "rss", "cursor_json": "{}"},
    ]

    result = collect_local(
        repository,
        client,
        command_run_id="local-due",
        due=True,
        channel_slug=None,
        collectors={"rss": LocalSuccess()},
    )

    assert result["status"] == "succeeded"
    assert [item["channel"] for item in result["channels"]] == ["composio-feed"]


def test_analysis_ingest_validates_before_worker_write():
    client = FakeClient()
    analysis = {
        "item_id": "item-1",
        "summary": "Summary",
        "key_change": "Changed",
        "why_it_matters": "Important",
        "company_impact": "Impact",
        "importance": 4,
        "confidence": 0.9,
        "topics": ["MCP"],
        "watch_next": ["Pricing"],
        "evidence": [{"url": "https://example.com/news", "claim": "Evidence"}],
    }
    result = ingest_analyses(
        client,
        payload=[analysis],
        command_run_id="run-2",
        external_run_id=None,
        model="codex",
        prompt_version="v1",
    )

    assert result["validated"] == 1
    record = next(call for call in client.calls if call[0] == "analyses")[1][0]
    assert record["model"] == "codex"
    assert record["item_id"] == "item-1"


def test_report_build_is_deterministic_for_generate_then_publish(tmp_path):
    _, repository = project(tmp_path)
    client = FakeClient()
    client.report_items = [
        {
            "id": "item-1",
            "title": "New API",
            "url": "https://example.com/news/new-api",
            "content_text": "A new API adds scoped authentication and audit logs for individual integrations.",
            "raw_metadata_json": '{"platform":"rss","content_complete":true}',
            "target_name": "Composio",
            "published_at": "2026-09-06T00:00:00Z",
            "summary": "Summary",
            "key_change": "Changed",
            "why_it_matters": "Important",
            "company_impact": "Impact",
            "importance": 4,
            "confidence": 0.9,
            "topics_json": '["MCP"]',
            "watch_next_json": '["Pricing"]',
            "evidence_json": '[{"url":"https://example.com/news","claim":"Evidence"}]',
        }
    ]
    options = dict(
        edition_value="morning",
        date_value="2026-09-06",
        from_value=None,
        to_value=None,
        title=None,
        description=None,
        trends=[],
        tag=None,
        target_slug=None,
    )
    report1, rendered1, _ = build_report(client, **options)
    report2, rendered2, _ = build_report(client, **options)

    assert report1.report_id == report2.report_id
    assert rendered1.markdown == rendered2.markdown

    # A first fetch of an undated page must not become an empty/noisy issue.
    client.report_items[0]["published_at"] = None
    client.report_items[0]["fetched_at"] = "2026-09-06T00:00:00Z"
    rejected_report, rejected_render, rejected_decision = build_report(client, **options)
    assert rejected_report.signals == ()
    assert rejected_render is None
    assert rejected_decision.should_generate is False


def test_weekly_report_identity_is_stable_within_iso_week(tmp_path):
    _, repository = project(tmp_path)
    client = FakeClient()
    client.report_items = [
        {
            "id": "item-1",
            "title": "New API",
            "url": "https://example.com/news/new-api",
            "content_text": "A new API adds scoped authentication and audit logs for individual integrations.",
            "target_name": "Composio",
            "published_at": "2026-09-03T00:00:00Z",
            "summary": "Summary",
            "key_change": "Changed",
            "why_it_matters": "Important",
            "company_impact": "Impact",
            "importance": 4,
            "confidence": 0.9,
            "topics_json": '["MCP"]',
            "watch_next_json": '["Pricing"]',
            "evidence_json": '[{"url":"https://example.com/news","claim":"Evidence"}]',
        }
    ]
    base = dict(
        edition_value="weekly",
        from_value=None,
        to_value=None,
        title=None,
        description=None,
        trends=[],
        tag=None,
        target_slug=None,
    )

    saturday, _, _ = build_report(client, date_value="2026-09-05", **base)
    sunday, _, _ = build_report(client, date_value="2026-09-06", **base)

    assert saturday.period == sunday.period == "2026-w36"
    assert saturday.report_id == sunday.report_id


def test_scheduler_dry_run_writes_nothing(tmp_path, monkeypatch):
    root, repository = project(tmp_path)
    destination = root / "agents"
    monkeypatch.setenv("INTELLIGENCE_LAUNCH_AGENTS_DIR", str(destination))

    result = scheduler_apply(repository, dry_run=True)

    assert result["dry_run"] is True
    assert not destination.exists()
    assert "1800" in result["rendered_plist"]


def test_report_windows_are_timezone_aware():
    start, end = report_window(ReportEdition.MORNING, __import__("datetime").date(2026, 9, 6))
    assert start.isoformat().endswith("+08:00")
    assert (end - start).total_seconds() == 13.5 * 3600


def test_failed_report_can_regenerate_unchanged_text(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from intelligence.cli import operations
    from intelligence.publisher import GateResult
    from intelligence.reporter import ReportStatus, render_hugo_report
    from dataclasses import replace
    from test_reporter_renderer import make_report

    _, repository = project(tmp_path)
    report = make_report()
    rendered = render_hugo_report(report)
    monkeypatch.setattr(operations, "build_report", lambda *a, **kw: (
        report, rendered, SimpleNamespace(should_generate=True)
    ))

    class ReplayClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.seen = set()
            self.state = None

        def create_report(self, payload, *, idempotency_key):
            if idempotency_key not in self.seen:
                self.seen.add(idempotency_key)
                self.state = "draft"

        def update_report_status(self, report_id, state, *, idempotency_key):
            if idempotency_key in self.seen:
                return
            assert (self.state, state) in {
                ("draft", "validating"), ("validating", "failed"), ("validating", "ready")
            }
            self.seen.add(idempotency_key)
            self.state = state

    attempts = iter([ReportStatus.FAILED, ReportStatus.READY])
    monkeypatch.setattr(operations.PublicationService, "validate", lambda *a, **kw:
        SimpleNamespace(report=replace(report, status=next(attempts)),
                        gates=(GateResult("sensitive_content", True, "checked"),)))
    client = ReplayClient()
    first = operations.generate_report(repository, client, command_run_id="attempt-1",
        dry_run=False, report_options={"edition_value": "morning"})
    second = operations.generate_report(repository, client, command_run_id="attempt-2",
        dry_run=False, report_options={"edition_value": "morning"})
    assert first["status"] == "failed"
    assert second["status"] == client.state == "ready"


def test_published_edition_skips_before_rebuilding_or_touching_artifact(tmp_path, monkeypatch):
    from intelligence.cli import operations
    _, repository = project(tmp_path)
    client = FakeClient()
    client.get_report = lambda report_id: {"report": {"report_status": "published", "published_url": "https://example.com"}}
    monkeypatch.setattr(operations, "build_report", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not rebuild")))
    generated = operations.generate_report(repository, client, command_run_id="repeat-generate",
        dry_run=False, report_options={"edition_value": "morning", "date_value": "2026-09-06"})
    published = operations.publish_report(repository, client, command_run_id="repeat-publish",
        execute=True, push=True, published_url="https://example.com", remote="origin", branch="main",
        report_options={"edition_value": "morning", "date_value": "2026-09-06"})
    assert generated["reason"] == published["reason"] == "already_published"
    assert generated["report_id"] == published["report_id"]
    assert [call[2]["run_status"] for call in client.calls if call[0] == "update_run"] == ["skipped", "skipped"]


def test_pending_analysis_preserves_prior_publication_context():
    from intelligence.cli.operations import pending_analysis
    client = FakeClient()
    context = [{"id": "prior", "summary": "Already explained this release", "canonical_url": "https://example.com/release"}]
    client.get_pending_analysis = lambda **kwargs: {"items": [{"id": "new-tweet"}], "recent_published_events": context}
    result = pending_analysis(client, command_run_id="context-run", limit=10, target_slug=None, channel_slug=None)
    assert result["recent_published_events"] == context
    assert result["items"] == [{"id": "new-tweet"}]
