from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .models import Report, ReportEdition, ReportSignal
from .policy import MAX_REPORT_SIGNALS


@dataclass(frozen=True)
class RenderedReport:
    relative_path: Path
    markdown: str


_EDITION_LABEL = {
    ReportEdition.MORNING: "早报",
    ReportEdition.MIDDAY: "午间快讯",
    ReportEdition.EVENING: "晚报",
    ReportEdition.WEEKLY: "战略周报",
    ReportEdition.AD_HOC: "专题报告",
}


def _yaml_string(value: str) -> str:
    # JSON strings are valid YAML double-quoted scalars and make escaping stable.
    return json.dumps(value, ensure_ascii=False)


def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9-]+", "-", value.lower()).strip("-")
    if not value:
        raise ValueError("period must contain at least one slug-safe character")
    return value


def _markdown_text(value: str) -> str:
    """Keep untrusted source/model text from becoming raw HTML or a shortcode."""

    return (
        value.replace("{{", "&#123;&#123;")
        .replace("}}", "&#125;&#125;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _inline_markdown(value: str, *, limit: int = 120) -> str:
    """Collapse untrusted multi-line titles into one readable Markdown line."""

    collapsed = " ".join(_markdown_text(value).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


_BOILERPLATE = (
    "反映相关产品与生态的演进",
    "结合自身路线评估影响",
    "持续关注后续动态",
    "值得持续关注",
    "需要进一步关注",
)
_INTERNAL_TAGS = {"official", "competitor", "social", "pricing", "high-signal", "product-update"}


def _headline(signal: ReportSignal) -> str:
    if signal.analysis.headline:
        return _inline_markdown(signal.analysis.headline, limit=60)
    summary = signal.analysis.summary.strip()
    # Prefer an informative Chinese sentence over an untranslated source title.
    title = re.split(r"[。！？\n]", summary, maxsplit=1)[0] if re.search(r"[\u4e00-\u9fff]", summary) else signal.title
    return _inline_markdown(title, limit=72)


def _source_links(signal: ReportSignal, *, primary_only: bool = False) -> str:
    sources = {source.url: source.title for source in signal.sources}
    # Evidence may cite a second primary source; keep it next to this event too.
    for evidence in signal.analysis.evidence:
        sources.setdefault(evidence.url, evidence.claim)
    urls = tuple(sources)
    if primary_only:
        urls = urls[:1]
    links = []
    for index, url in enumerate(urls):
        label = (
            "数据源：" + _inline_markdown(signal.source_label, limit=80)
            if index == 0 and signal.source_label
            else ("原文" if index == 0 else f"补充来源 {index + 1}")
        )
        links.append(f"[{label}]({url})")
    return " · ".join(links)


def _signal_markdown(signal: ReportSignal, report: Report) -> list[str]:
    analysis = signal.analysis
    lines = [f"### {_headline(signal)}", "", _inline_markdown(analysis.summary, limit=320), ""]
    change = analysis.key_change.strip()
    if change and change != analysis.summary.strip() and not any(text in change for text in _BOILERPLATE):
        lines.extend([_inline_markdown(change, limit=180), ""])
    impact = analysis.why_it_matters.strip()
    if impact and impact != analysis.summary.strip() and not any(text in impact for text in _BOILERPLATE):
        lines.extend([f"**读者价值：** {_inline_markdown(impact, limit=180)}", ""])
    actions = [a for a in analysis.watch_next if not any(p in a for p in ("关注后续", "继续关注", "核验后续更新", "评估其影响"))]
    if actions:
        lines.extend([f"**可以做什么：** {_inline_markdown(actions[0], limit=120)}", ""])
    timing = "观察到页面变化 · " if signal.date_kind == "observed_change" else ("近期补读 · " if signal.published_at < report.window_start else "")
    lines.extend([f"{timing}{signal.published_at.astimezone(report.generated_at.tzinfo).date().isoformat()} · {_source_links(signal)}", ""])
    return lines


def render_hugo_report(report: Report) -> RenderedReport:
    """Render a byte-stable Chinese Hugo report from validated structures."""

    if not report.signals:
        raise ValueError("cannot render a report without signals")

    weekly = report.edition is ReportEdition.WEEKLY
    # The policy orders events by importance. Deduplicate before applying the
    # reading budget so an event cannot appear in both detail and briefs.
    selected = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for signal in report.signals:
        urls = {source.url for source in signal.sources}
        if signal.item_id in seen_ids or urls & seen_urls:
            continue
        selected.append(signal)
        seen_ids.add(signal.item_id)
        seen_urls.update(urls)
        if len(selected) == MAX_REPORT_SIGNALS:
            break
    if report.edition in {ReportEdition.WEEKLY, ReportEdition.AD_HOC}:
        primary_signals = tuple(selected[:3])
    else:
        primary_signals = tuple(signal for signal in selected if signal.analysis.importance >= 3)[:3]
    primary_ids = {signal.item_id for signal in primary_signals}
    briefs = tuple(signal for signal in selected if signal.item_id not in primary_ids)
    overview_signals = tuple(selected[:3])
    tags = sorted(
        {
            value
            for signal in selected
            for value in (signal.target, *signal.analysis.topics)
            if value.casefold() not in _INTERNAL_TAGS
            and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", value)
        },
        key=lambda value: (value.casefold(), value),
    )[:5]
    source_by_url = {
        source.url: source
        for signal in selected
        for source in signal.sources
    }
    source_urls = set(source_by_url) | {e.url for signal in selected for e in signal.analysis.evidence}

    front_matter = [
        "---",
        f"title: {_yaml_string(report.title)}",
        f"date: {_yaml_string(report.generated_at.isoformat())}",
        'categories: ["Intelligence"]',
        "tags: [" + ", ".join(_yaml_string(tag) for tag in tags) + "]",
        f"description: {_yaml_string(report.description)}",
        f"reportType: {_yaml_string(report.edition.value)}",
        f"period: {_yaml_string(report.period)}",
        "generated: true",
        "isCJKLanguage: true",
        f"sourcesCount: {len(source_urls)}",
        f"hiddenInHomeList: {'false' if weekly else 'true'}",
        f"reportId: {_yaml_string(report.report_id)}",
        "---",
        "",
    ]

    body = [f"本期 {len(primary_signals)} 条重点" + (f"、{len(briefs)} 条快讯。" if briefs else "。"), ""]
    if any(s.published_at < report.window_start for s in selected):
        body.extend(["标注“近期补读”的内容在本期窗口之前发布；本次按实际日期补充收录，不当作今日新消息。", ""])
    body.extend(["## 30 秒速览", ""])
    for signal in overview_signals:
        label = "【近期补读】" if signal.published_at < report.window_start else ""
        body.append(f"- {label}{_headline(signal)}。{_source_links(signal, primary_only=True)}")
    if primary_signals:
        body.extend(["", f"## {_EDITION_LABEL[report.edition]}重点", ""])
        for signal in primary_signals:
            body.extend(_signal_markdown(signal, report))

    if briefs:
        body.extend(["## 一句话快讯", ""])
        body.extend(
            f"- {'【近期补读】' if signal.published_at < report.window_start else ''}{_inline_markdown(signal.analysis.summary, limit=150)} 来源：{_source_links(signal)}"
            for signal in briefs
        )
    body.append("")

    filename = f"{_slug(report.period)}-{report.edition.value}.zh.md"
    from intelligence.publisher.verification import publication_marker

    return RenderedReport(
        relative_path=Path("content/posts/intelligence") / filename,
        markdown=publication_marker("\n".join(front_matter + body)),
    )
