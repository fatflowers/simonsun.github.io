"""Conservative report eligibility; discovery records are not news events.

This catches deterministic defects, not factual truth. Editorial analysis must
still verify the source. Observation time is usable only with a verified
before/after snapshot difference; it is never an inferred publication time.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit


def verified_observed_change(metadata: Any) -> Mapping[str, Any] | None:
    if not isinstance(metadata, Mapping):
        return None
    diff = metadata.get("web_diff")
    if not isinstance(diff, Mapping):
        return None
    before_hash, after_hash = diff.get("before_hash"), diff.get("after_hash")
    if not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) for value in (before_hash, after_hash)):
        return None
    before, after = diff.get("before_text"), diff.get("after_text")
    if before_hash == after_hash or not isinstance(before, str) or not isinstance(after, str):
        return None
    if not before.strip() or not after.strip() or before == after:
        return None
    try:
        observed = datetime.fromisoformat(str(diff.get("observed_at", "")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return diff if observed.tzinfo is not None else None


def exclusion_reason(row: Mapping[str, Any], start: datetime, end: datetime) -> str | None:
    if row.get("is_baseline") in (True, 1, "1"):
        return "baseline"
    raw = row.get("raw_metadata_json", row.get("raw_metadata", {}))
    try:
        metadata = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return "invalid_metadata"
    if isinstance(metadata, Mapping) and metadata.get("discovery_only"):
        return "discovery_only"
    observed_change = verified_observed_change(metadata)
    if isinstance(metadata, Mapping) and metadata.get("date_kind") == "observed_change" and not observed_change:
        return "unverified_observed_change"
    for key in ("canonical_url", "url"):
        if row.get(key) and is_discovery_url(str(row[key])) and not observed_change:
            return "discovery_url"
    value = observed_change["observed_at"] if observed_change else row.get("published_at")
    if not value:
        return "unknown_publication_time"
    try:
        published = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if published.tzinfo is None:
            return "unknown_publication_timezone"
        if not start <= published < end:
            return "outside_report_window"
    except ValueError:
        return "invalid_publication_time"
    if not observed_change:
        source = str(metadata.get("publication_date_source", "")) if isinstance(metadata, Mapping) else ""
        platform = str(metadata.get("platform", "")) if isinstance(metadata, Mapping) else ""
        if source not in {"article_metadata", "article_text", "feed", "platform"} and platform not in {"twitter", "rss", "github", "mcp_registry"}:
            return "unverified_publication_evidence"
    body = str(row.get("content_text") or "").strip()
    title = str(row.get("title") or "").strip()
    # Short social posts can be useful; title-only search results cannot.
    complete_social = isinstance(metadata, Mapping) and metadata.get("source_content_kind") == "complete_social_post"
    if len(body) < 40 or (body.casefold() == title.casefold() and not complete_social):
        return "insufficient_source_content"
    if re.search(r"(?:…|\.\.\.)\s*(?:https?://\S+)?\s*$", body):
        return "truncated_source_content"
    summary = str(row.get("summary") or "").strip()
    if not summary or summary.casefold().rstrip("。.") == title.casefold().rstrip("。."):
        return "title_only_summary"
    if re.search(r"(?:…|\.\.\.)\s*$", summary):
        return "truncated_summary"
    placeholders = ("公开来源显示", "反映相关产品与生态的演进", "结合自身路线评估影响",
                    "持续关注后续动态", "暂无具体信息", "待进一步分析")
    fields = (summary, str(row.get("key_change") or ""), str(row.get("why_it_matters") or ""))
    if any(phrase in field for phrase in placeholders for field in fields):
        return "placeholder_analysis"
    self_disqualifying = (
        "没有必要再次刊登", "没有新增事实", "重复已刊", "已刊旧事件", "旧口径",
        "合并介绍",
    )
    if any(phrase in field for phrase in self_disqualifying for field in fields):
        return "analysis_marks_item_incomplete_or_duplicate"
    return None


def is_discovery_url(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        return True
    path = parsed.path.strip("/").lower()
    parts = path.split("/") if path else []
    if not parts:
        return True
    if parsed.hostname.lower() in {"github.com", "www.github.com"}:
        if len(parts) <= 2 or (len(parts) == 3 and parts[2] in {"releases", "tags", "issues", "pulls"}):
            return True
        if len(parts) >= 5 and parts[2:4] == ["releases", "tag"]:
            return False
    if path in {"blog", "blogs", "news", "posts", "articles", "docs", "documentation", "changelog", "releases"}:
        return True
    if re.search(r"(?:^|/)(?:page/\d+|tags?(?:/.*)?|categories(?:/.*)?|index\.(?:html?|php))$", path):
        return True
    return any(key in parse_qs(parsed.query) for key in ("page", "paged", "offset"))
