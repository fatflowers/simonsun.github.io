from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from intelligence.analyzer import validate_analysis
from intelligence.reporter import (
    Report,
    ReportEdition,
    ReportLifecycleError,
    ReportSignal,
    ReportSource,
    ReportStatus,
    render_hugo_report,
)


def make_report(*, edition: ReportEdition = ReportEdition.MORNING) -> Report:
    source = ReportSource("https://example.com/news", "官方公告")
    analysis = validate_analysis(
        {
            "summary": "发布了新的 Agent 能力。",
            "key_change": "新增稳定接口。",
            "why_it_matters": "降低集成成本。",
            "company_impact": "Aisa 可评估兼容能力。",
            "importance": 4,
            "confidence": 0.87,
            "topics": ["MCP", "Agent"],
            "watch_next": ["定价"],
            "evidence": [{"url": source.url, "claim": "官方宣布新接口"}],
        }
    )
    now = datetime(2026, 9, 5, 8, 30, tzinfo=timezone.utc)
    return Report(
        report_id="report-1",
        edition=edition,
        period="2026-09-05" if edition is not ReportEdition.WEEKLY else "2026-W36",
        generated_at=now,
        window_start=datetime(2026, 9, 4, 19, tzinfo=timezone.utc),
        window_end=now,
        title="AI 情报早报｜2026-09-05",
        description="今日值得关注的公开信号",
        signals=(ReportSignal("item-1", "Composio", "新接口", now, analysis, (source,)),),
        trends=("Agent 工具接口趋于标准化。",),
    )


def test_render_is_deterministic_and_has_required_front_matter() -> None:
    report = make_report()
    first = render_hugo_report(report)
    second = render_hugo_report(report)
    assert first == second
    assert first.relative_path.as_posix() == "content/posts/intelligence/2026-09-05-morning.zh.md"
    assert 'reportType: "morning"' in first.markdown
    assert "sourcesCount: 1" in first.markdown
    assert "isCJKLanguage: true" in first.markdown
    assert "hiddenInHomeList: true" in first.markdown
    assert "## 30 秒速览" in first.markdown
    assert "## 早报重点" in first.markdown
    assert "[原文](https://example.com/news)" in first.markdown


def test_weekly_report_appears_on_home_page() -> None:
    rendered = render_hugo_report(make_report(edition=ReportEdition.WEEKLY))
    assert "hiddenInHomeList: false" in rendered.markdown
    assert rendered.relative_path.name == "2026-w36-weekly.zh.md"


def test_explicit_headline_used_without_changing_source_title_or_summary() -> None:
    report = make_report()
    signal = report.signals[0]
    signal = replace(signal, analysis=replace(signal.analysis, headline="Composio 新增稳定接口"))
    rendered = render_hugo_report(replace(report, signals=(signal,)))
    assert "### Composio 新增稳定接口" in rendered.markdown
    assert signal.analysis.summary in rendered.markdown
    assert signal.title == "新接口"
    assert "[原文](https://example.com/news)" in rendered.markdown


def test_lifecycle_enforces_publish_sequence() -> None:
    report = make_report()
    with pytest.raises(ReportLifecycleError):
        report.mark_published(commit_sha="abc", published_url="https://example.com")
    validating = report.transition(ReportStatus.VALIDATING)
    ready = validating.transition(ReportStatus.READY)
    published = ready.mark_published(commit_sha="abc", published_url="https://example.com")
    assert published.status is ReportStatus.PUBLISHED
    assert published.commit_sha == "abc"


def test_empty_report_cannot_render() -> None:
    report = make_report()
    with pytest.raises(ValueError, match="without signals"):
        render_hugo_report(
            Report(
                report_id=report.report_id,
                edition=report.edition,
                period=report.period,
                generated_at=report.generated_at,
                window_start=report.window_start,
                window_end=report.window_end,
                title=report.title,
                description=report.description,
                signals=(),
            )
        )


def test_renderer_escapes_raw_html_and_hugo_shortcodes() -> None:
    report = make_report()
    analysis = replace(report.signals[0].analysis, summary="<script>x</script> {{< bad >}}")
    signal = replace(report.signals[0], analysis=analysis)
    markdown = render_hugo_report(replace(report, signals=(signal,))).markdown
    assert "<script>" not in markdown
    assert "{{<" not in markdown
    assert "&lt;script&gt;" in markdown


def test_renderer_collapses_multiline_titles_and_source_labels() -> None:
    report = make_report()
    source = ReportSource("https://example.com/thread", "第一行\n\n- 第二行")
    signal = replace(
        report.signals[0],
        title="第一行\n\n- 第二行",
        sources=(source,),
        analysis=replace(
            report.signals[0].analysis,
            evidence=(replace(report.signals[0].analysis.evidence[0], url=source.url),),
        ),
    )

    markdown = render_hugo_report(replace(report, signals=(signal,))).markdown

    assert "### 发布了新的 Agent 能力" in markdown
    assert "[原文](https://example.com/thread)" in markdown
    assert "- 第二行" not in markdown


def test_renderer_uses_short_source_labels_even_for_long_titles() -> None:
    report = make_report()
    long_title = "很长的标题" * 40
    source = ReportSource("https://example.com/long", long_title)
    signal = replace(
        report.signals[0],
        title=long_title,
        sources=(source,),
        analysis=replace(
            report.signals[0].analysis,
            evidence=(replace(report.signals[0].analysis.evidence[0], url=source.url),),
        ),
    )

    markdown = render_hugo_report(replace(report, signals=(signal,))).markdown

    source_line = next(line for line in markdown.splitlines() if "2026-09-05 · [原文]" in line)
    assert "](https://example.com/long)" in source_line
    assert "[原文](https://example.com/long)" in source_line
    assert long_title not in markdown


def test_reading_budget_inline_sources_and_no_source_dump() -> None:
    report = make_report()
    original = report.signals[0]
    signals = tuple(
        replace(
            original,
            item_id=f"item-{index}",
            sources=(ReportSource(f"https://example.com/{index}", f"公告 {index}"),),
            analysis=replace(
                original.analysis,
                summary=f"第 {index} 个产品新增稳定接口。",
                evidence=(replace(original.analysis.evidence[0], url=f"https://example.com/{index}"),),
            ),
        )
        for index in range(12)
    )
    markdown = render_hugo_report(replace(report, signals=(signals[0], *signals))).markdown
    assert markdown.count("### ") == 3
    briefs = markdown.split("## 一句话快讯")[1]
    assert len([line for line in briefs.splitlines() if line.startswith("- ")]) == 9
    for index in range(3, 12):
        assert f"来源：[原文](https://example.com/{index})" in briefs
    assert "sourcesCount: 12" in markdown
    assert "https://example.com/11" in markdown
    for forbidden in ("## 来源", "对 Aisa", "置信度", "★", "继续观察", "## 趋势变化"):
        assert forbidden not in markdown


def test_reader_tags_and_generic_impact_are_filtered() -> None:
    report = make_report()
    signal = replace(report.signals[0], analysis=replace(
        report.signals[0].analysis,
        topics=("official", "competitor", "agent-resource-layer", "MCP", "Agent", "SDK", "API", "Tools", "模型"),
        why_it_matters="反映相关产品与生态的演进，结合自身路线评估影响。",
    ))
    markdown = render_hugo_report(replace(report, signals=(signal,))).markdown
    import json
    tags = json.loads(next(line.removeprefix("tags: ") for line in markdown.splitlines() if line.startswith("tags: ")))
    assert len(tags) == 5
    assert not {"official", "competitor", "agent-resource-layer"} & set(tags)
    assert "读者价值" not in markdown


def test_additional_evidence_is_linked_with_its_event() -> None:
    report = make_report()
    signal = replace(report.signals[0], analysis=replace(
        report.signals[0].analysis,
        evidence=(*report.signals[0].analysis.evidence, replace(
            report.signals[0].analysis.evidence[0], url="https://example.com/spec", claim="接口兼容性说明",
        )),
    ))
    markdown = render_hugo_report(replace(report, signals=(signal,))).markdown
    detail = markdown.split("### ", 1)[1]
    assert "[补充来源 2](https://example.com/spec)" in detail
    overview = markdown.split("## 早报重点", 1)[0]
    assert "https://example.com/news" in overview
    assert "https://example.com/spec" not in overview
    assert "sourcesCount: 2" in markdown


def test_report_displays_target_and_channel_as_data_source() -> None:
    report = make_report()
    signal = replace(report.signals[0], source_label="Composio / Official Blog")
    markdown = render_hugo_report(replace(report, signals=(signal,))).markdown
    assert "[数据源：Composio / Official Blog](https://example.com/news)" in markdown


def test_importance_two_daily_signal_is_brief_not_lead() -> None:
    report = make_report()
    low = replace(
        report.signals[0],
        item_id="item-low",
        analysis=replace(report.signals[0].analysis, importance=2),
    )
    markdown = render_hugo_report(replace(report, signals=(low,))).markdown
    assert "本期 0 条重点、1 条快讯" in markdown
    assert "## 早报重点" not in markdown
    assert "## 一句话快讯" in markdown
    assert low.analysis.summary in markdown


def test_daily_report_can_render_one_hundred_briefs_when_there_are_no_leads() -> None:
    report = make_report()
    original = report.signals[0]
    signals = tuple(
        replace(
            original,
            item_id=f"brief-{index}",
            sources=(ReportSource(f"https://example.com/brief-{index}", f"来源 {index}"),),
            analysis=replace(
                original.analysis,
                importance=2,
                summary=f"第 {index} 条有效快讯。",
                evidence=(replace(original.analysis.evidence[0], url=f"https://example.com/brief-{index}"),),
            ),
        )
        for index in range(100)
    )
    markdown = render_hugo_report(replace(report, signals=signals)).markdown
    assert "本期 0 条重点、100 条快讯" in markdown
    assert "## 早报重点" not in markdown
    assert len([line for line in markdown.split("## 一句话快讯", 1)[1].splitlines() if line.startswith("- ")]) == 100
