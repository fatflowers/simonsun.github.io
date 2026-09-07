from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from urllib.parse import urlsplit
from typing import Union
from intelligence.normalize.text import canonicalize_url

from intelligence.models.runs import RunStatus

from .models import ReportEdition, ReportSignal

MAX_REPORT_SIGNALS = 100


def _editorial_text(signal: ReportSignal) -> str:
    value = " ".join(filter(None, (
        signal.analysis.headline or "", signal.analysis.summary, signal.analysis.key_change,
    ))).casefold()
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value)


def _semantic_duplicate(left: ReportSignal, right: ReportSignal) -> bool:
    if left.target.casefold() != right.target.casefold():
        return False
    a, b = _editorial_text(left), _editorial_text(right)
    if not a or not b:
        return False
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= 32 and shorter in longer:
        return True
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.82


def _same_social_thread(left: ReportSignal, right: ReportSignal) -> bool:
    if left.target.casefold() != right.target.casefold():
        return False
    if abs((left.published_at - right.published_at).total_seconds()) > 30 * 60:
        return False
    identities = []
    for signal in (left, right):
        urls = [urlsplit(source.url) for source in signal.sources]
        social = [url for url in urls if (url.hostname or "").lower() in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}]
        identities.append(social[0].path.strip("/").split("/", 1)[0].casefold() if social else "")
    return bool(identities[0] and identities[0] == identities[1])


def reading_budget(signals):
    """Give the overview distinct subjects when comparable useful events exist."""
    primary, seen = [], set()
    for signal in signals:
        if signal.target not in seen:
            primary.append(signal)
            seen.add(signal.target)
        if len(primary) == 3:
            break
    return tuple((primary + [s for s in signals if s not in primary])[:MAX_REPORT_SIGNALS])


@dataclass(frozen=True)
class PolicyDecision:
    should_generate: bool
    selected: tuple[ReportSignal, ...]
    deferred: tuple[ReportSignal, ...]
    reason: str
    run_status: RunStatus


class ReportPolicy:
    """Deterministic daily and weekly report selection policy."""

    def __init__(self, *, midday_min_importance: int = 4, low_priority_max: int = 1):
        if not 1 <= midday_min_importance <= 5:
            raise ValueError("midday_min_importance must be between 1 and 5")
        self.midday_min_importance = midday_min_importance
        self.low_priority_max = low_priority_max

    def decide(
        self,
        edition: ReportEdition,
        signals: Union[tuple[ReportSignal, ...], list[ReportSignal]],
    ) -> PolicyDecision:
        ordered = tuple(
            sorted(
                signals,
                key=lambda signal: (
                    -signal.analysis.importance,
                    -signal.analysis.confidence,
                    signal.published_at,
                    signal.item_id,
                ),
            )
        )
        # Deduplicate BEFORE spending the reading budget. Shared primary
        # evidence identifies social/blog retellings of the same announcement.
        unique = []
        seen_urls = set()
        seen_ids = set()
        for signal in ordered:
            urls = {canonicalize_url(source.url) for source in signal.sources}
            if signal.item_id in seen_ids or (urls and urls & seen_urls) or any(
                _semantic_duplicate(signal, prior) or _same_social_thread(signal, prior)
                for prior in unique
            ):
                continue
            unique.append(signal)
            seen_urls.update(urls)
            seen_ids.add(signal.item_id)
        ordered = tuple(unique)
        if not ordered:
            return PolicyDecision(False, (), (), "no_new_content", RunStatus.SKIPPED)

        if edition is ReportEdition.MIDDAY:
            selected = reading_budget(tuple(
                signal
                for signal in ordered
                if signal.analysis.importance >= self.midday_min_importance
            ))
            deferred = tuple(signal for signal in ordered if signal not in selected)
            if not selected:
                return PolicyDecision(False, (), deferred, "no_high_importance_signal", RunStatus.SKIPPED)
            return PolicyDecision(True, selected, deferred, "high_importance_signal", RunStatus.SUCCEEDED)

        if edition in {ReportEdition.MORNING, ReportEdition.EVENING}:
            high_value = tuple(
                signal for signal in ordered if signal.analysis.importance > self.low_priority_max
            )
            if not high_value:
                return PolicyDecision(False, (), ordered, "no_publishable_signal", RunStatus.SKIPPED)
            selected = reading_budget(high_value)
            deferred = tuple(signal for signal in ordered if signal not in selected)
            return PolicyDecision(True, selected, deferred, "new_content", RunStatus.SUCCEEDED)

        if edition is ReportEdition.WEEKLY:
            selected = reading_budget(ordered)
            return PolicyDecision(True, selected, tuple(s for s in ordered if s not in selected), "weekly_window_has_content", RunStatus.SUCCEEDED)

        return PolicyDecision(True, ordered[:MAX_REPORT_SIGNALS], ordered[MAX_REPORT_SIGNALS:], "ad_hoc_requested", RunStatus.SUCCEEDED)
