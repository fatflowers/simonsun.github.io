from __future__ import annotations

from datetime import datetime, timezone

from intelligence.analyzer import validate_analysis
from intelligence.reporter import ReportEdition, ReportPolicy, ReportSignal, ReportSource


def signal(item_id: str, importance: int) -> ReportSignal:
    analysis = validate_analysis(
        {
            "summary": f"摘要 {item_id}",
            "key_change": "变化",
            "why_it_matters": "重要原因",
            "company_impact": "影响",
            "importance": importance,
            "confidence": 0.8,
            "topics": ["MCP"],
            "watch_next": ["定价"],
            "evidence": [{"url": f"https://example.com/{item_id}", "claim": "证据"}],
        }
    )
    return ReportSignal(
        item_id=item_id,
        target="Composio",
        title=f"事件 {item_id}",
        published_at=datetime(2026, 9, 5, int(item_id) % 24, tzinfo=timezone.utc),
        analysis=analysis,
        sources=(ReportSource(f"https://example.com/{item_id}", f"来源 {item_id}"),),
    )


def test_no_new_content_is_skipped_for_every_edition() -> None:
    policy = ReportPolicy()
    for edition in ReportEdition:
        decision = policy.decide(edition, [])
        assert decision.should_generate is False
        assert decision.run_status == "skipped"
        assert decision.reason == "no_new_content"


def test_midday_selects_only_four_star_or_higher() -> None:
    decision = ReportPolicy().decide(
        ReportEdition.MIDDAY,
        [signal("1", 3), signal("2", 5), signal("3", 4)],
    )
    assert decision.should_generate
    assert [item.item_id for item in decision.selected] == ["2", "3"]
    assert [item.item_id for item in decision.deferred] == ["1"]


def test_daily_importance_two_is_published() -> None:
    decision = ReportPolicy().decide(ReportEdition.MORNING, [signal("1", 2)])
    assert decision.should_generate
    assert decision.selected[0].item_id == "1"


def test_weekly_orders_signals_by_importance() -> None:
    decision = ReportPolicy().decide(
        ReportEdition.WEEKLY,
        [signal("1", 3), signal("2", 5), signal("3", 4)],
    )
    assert decision.should_generate
    assert [item.item_id for item in decision.selected] == ["2", "3", "1"]


def test_daily_caps_total_reading_budget_at_one_hundred() -> None:
    decision = ReportPolicy().decide(
        ReportEdition.MORNING,
        [signal(str(i), 4) for i in range(1, 101)] + [signal("101", 2)],
    )
    assert len(decision.selected) == 100
    assert len(decision.deferred) == 1
    assert decision.deferred[0].analysis.importance == 2
