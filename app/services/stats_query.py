"""관리자 통계 — 카테고리별 건수·평균 처리시간·SLA 준수율 (작업 1-16, 명세서 4-2).

계산 기준 (2026-10-05 규민, status.md 4장 제안):
- 기간: 접수 시각(created_at)이 기간 안인 신고. days가 없으면 전체 기간.
- 처리시간 = 마지막으로 '해결'이 된 시각(report_status_history) − 접수 시각. 현재 상태가 해결·종료인
  신고만 잼. 재오픈(해결 → 처리중)됐다가 다시 해결되면 마지막 해결 시각 기준.
- SLA 준수율 = 준수 / (준수 + 위반) × 100.
    준수: 해결·종료 + 해결 시각 ≤ sla_deadline
    위반: 해결·종료 + 해결 시각 > sla_deadline, 또는 처리 중(접수·배정·처리중)인데 이미 마감 지남
    제외: 아직 마감 전인 처리 중 건(결과 미정), sla_deadline이 없는 건, 해결 이력이 없는 해결 건
  처리 중 초과 건을 위반에 넣는 이유: 빼면 오래 방치된 건이 많을수록 준수율이 오히려 좋아 보임.
- 마감은 접수 때 정한 sla_deadline 그대로 (재오픈해도 다시 계산 안 함 — 명세 10-2와 같음).

신고 수가 월 1,000건 수준(명세 S-9 기준)이라 행을 읽어 파이썬에서 묶는다 — 기준을 한 곳에서
읽기 쉽게 하려는 선택. 수만 건 단위가 되면 SQL GROUP BY로 옮기면 됨.
"""
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.config import Category
from app.models.enums import Priority, ReportStatus
from app.models.report import Report, ReportStatusHistory
from app.schemas.admin import NamedRef
from app.schemas.stats import (
    AdminStats,
    CategoryStats,
    PriorityStats,
    SlaSummary,
    StatsGroup,
    StatsPeriod,
    StatusCount,
)
from app.services.report_query import OPEN_STATUSES

DONE_STATUSES = (ReportStatus.RESOLVED, ReportStatus.CLOSED)


@dataclass(frozen=True)
class _Row:
    category_id: uuid.UUID | None
    priority: Priority
    status: ReportStatus
    created_at: datetime
    sla_deadline: datetime | None
    resolved_at: datetime | None  # 마지막 '해결' 전이 시각 (없으면 None)


def sla_outcome(row: _Row, now: datetime) -> bool | None:
    """True = 준수, False = 위반, None = 셈에서 제외 (위 모듈 주석의 기준)."""
    if row.sla_deadline is None:
        return None
    if row.status in DONE_STATUSES:
        if row.resolved_at is None:
            return None
        return row.resolved_at <= row.sla_deadline
    if row.status in OPEN_STATUSES and now >= row.sla_deadline:
        return False
    return None


def _group(rows: Iterable[_Row], now: datetime) -> StatsGroup:
    count = 0
    durations: list[float] = []
    met = breached = 0
    for r in rows:
        count += 1
        if r.status in DONE_STATUSES and r.resolved_at is not None:
            durations.append((r.resolved_at - r.created_at).total_seconds() / 3600)
        outcome = sla_outcome(r, now)
        if outcome is True:
            met += 1
        elif outcome is False:
            breached += 1
    judged = met + breached
    return StatsGroup(
        count=count,
        resolved_count=len(durations),
        avg_resolution_hours=round(sum(durations) / len(durations), 1) if durations else None,
        sla=SlaSummary(
            met=met,
            breached=breached,
            compliance_pct=round(met / judged * 100, 1) if judged else None,
        ),
    )


def get_stats(db: Session, days: int | None = None, now: datetime | None = None) -> AdminStats:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days) if days is not None else None

    last_resolved = (
        select(
            ReportStatusHistory.report_id.label("report_id"),
            func.max(ReportStatusHistory.changed_at).label("resolved_at"),
        )
        .where(ReportStatusHistory.to_status == ReportStatus.RESOLVED)
        .group_by(ReportStatusHistory.report_id)
        .subquery()
    )
    stmt = select(
        Report.category_id,
        Report.priority,
        Report.status,
        Report.created_at,
        Report.sla_deadline,
        last_resolved.c.resolved_at,
    ).outerjoin(last_resolved, last_resolved.c.report_id == Report.id)
    if since is not None:
        stmt = stmt.where(Report.created_at >= since)
    rows = [_Row(*r) for r in db.execute(stmt).all()]

    by_status = [
        StatusCount(status=s, count=sum(1 for r in rows if r.status == s)) for s in ReportStatus
    ]
    by_priority = [
        PriorityStats(priority=p, **_group((r for r in rows if r.priority == p), now).model_dump())
        for p in Priority
    ]
    used = {r.category_id for r in rows}
    categories = [
        c for c in db.scalars(select(Category)).all() if c.is_active or c.id in used
    ]
    by_category = [
        CategoryStats(
            category=NamedRef(id=c.id, name=c.name),
            is_active=c.is_active,
            **_group((r for r in rows if r.category_id == c.id), now).model_dump(),
        )
        for c in categories
    ]
    by_category.sort(key=lambda s: (-s.count, s.category.name))

    return AdminStats(
        period=StatsPeriod(days=days, since=since, until=now),
        total=_group(rows, now),
        by_status=by_status,
        by_category=by_category,
        by_priority=by_priority,
    )
