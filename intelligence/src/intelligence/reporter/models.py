from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Optional

from intelligence.analyzer import AnalysisResult


class ReportEdition(str, Enum):
    MORNING = "morning"
    MIDDAY = "midday"
    EVENING = "evening"
    WEEKLY = "weekly"
    AD_HOC = "ad-hoc"


class ReportStatus(str, Enum):
    DRAFT = "draft"
    VALIDATING = "validating"
    READY = "ready"
    PUBLISHED = "published"
    FAILED = "failed"


class ReportLifecycleError(ValueError):
    pass


_TRANSITIONS = {
    ReportStatus.DRAFT: {ReportStatus.VALIDATING},
    ReportStatus.VALIDATING: {ReportStatus.READY, ReportStatus.FAILED},
    ReportStatus.READY: {ReportStatus.PUBLISHED, ReportStatus.FAILED},
    ReportStatus.PUBLISHED: set(),
    ReportStatus.FAILED: {ReportStatus.VALIDATING},
}


@dataclass(frozen=True)
class ReportSource:
    url: str
    title: str
    is_public: bool = True


@dataclass(frozen=True)
class ReportSignal:
    item_id: str
    target: str
    title: str
    published_at: datetime
    analysis: AnalysisResult
    sources: tuple[ReportSource, ...]
    date_kind: str = "published"
    source_label: str = ""


@dataclass(frozen=True)
class Report:
    report_id: str
    edition: ReportEdition
    period: str
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    title: str
    description: str
    signals: tuple[ReportSignal, ...]
    trends: tuple[str, ...] = ()
    status: ReportStatus = ReportStatus.DRAFT
    status_reason: Optional[str] = None
    commit_sha: Optional[str] = None
    published_url: Optional[str] = None
    metadata: dict[str, str] = field(default_factory=dict)

    def transition(self, new_status: ReportStatus, *, reason: Optional[str] = None) -> "Report":
        if new_status not in _TRANSITIONS[self.status]:
            raise ReportLifecycleError(
                f"invalid report status transition: {self.status.value} -> {new_status.value}"
            )
        return replace(self, status=new_status, status_reason=reason)

    def mark_published(self, *, commit_sha: str, published_url: str) -> "Report":
        if self.status is not ReportStatus.READY:
            raise ReportLifecycleError("only a ready report can be published")
        if not commit_sha or not published_url:
            raise ReportLifecycleError("publishing requires a commit SHA and URL")
        return replace(
            self,
            status=ReportStatus.PUBLISHED,
            status_reason=None,
            commit_sha=commit_sha,
            published_url=published_url,
        )
