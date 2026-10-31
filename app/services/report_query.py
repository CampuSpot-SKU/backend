"""관리자용 신고 조회 — 목록(필터·정렬)과 상세 (작업 1-6)."""
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, DateTime, Select, and_, func, literal, select
from sqlalchemy.orm import Session

from app.models.admin import Admin
from app.models.config import Building, Category
from app.models.enums import ClusterStatus, Priority, ReportStatus
from app.models.problem import ProblemCluster, problem_cluster_reports
from app.models.report import Report, ReportStatusHistory
from app.schemas.admin import (
    AdminReportDetail,
    AdminReportItem,
    AdminReportList,
    NamedRef,
    ReportClusterRef,
    ReportSummary,
    SlaStatus,
    SortKey,
    StatusHistoryItem,
)

# ai 탐지가 호실이 다른 신고를 건물 전체 원인으로 묶을 때 쓰는 세부위치 (ai/detection.py WHOLE_BUILDING과 같은 값)
WHOLE_BUILDING = "건물 전체"

# SLA를 따지는 상태 (해결·종료는 이미 끝난 건이라 제외)
OPEN_STATUSES = (ReportStatus.RECEIVED, ReportStatus.ASSIGNED, ReportStatus.IN_PROGRESS)


def sla_status_of(
    status: ReportStatus, created_at: datetime, deadline: datetime | None, now: datetime
) -> SlaStatus | None:
    """한 건의 SLA 상태. 아래 _sla_filter(SQL)와 반드시 같은 기준이어야 함."""
    if status not in OPEN_STATUSES or deadline is None:
        return None
    if now >= deadline:
        return "초과"
    if now - created_at >= deadline - now:  # 지난 시간 ≥ 남은 시간 = SLA 절반 이상 경과
        return "임박"
    return "온타임"


def _sla_filter(sla: SlaStatus, now: datetime) -> ColumnElement[bool]:
    open_ = and_(Report.status.in_(OPEN_STATUSES), Report.sla_deadline.is_not(None))
    now_ = literal(now, DateTime(timezone=True))
    past_half = (now_ - Report.created_at) >= (Report.sla_deadline - now_)  # 지난 시간 ≥ 남은 시간
    if sla == "초과":
        return and_(open_, Report.sla_deadline <= now_)
    if sla == "임박":
        return and_(open_, Report.sla_deadline > now_, past_half)
    return and_(open_, Report.sla_deadline > now_, ~past_half)


def _base() -> Select[Report, Category, Building]:
    return (
        select(Report, Category, Building)
        .join(Category, Category.id == Report.category_id)
        .outerjoin(Building, Building.id == Report.building_id)
    )


def _item_fields(
    report: Report, category: Category, building: Building | None, now: datetime
) -> dict[str, Any]:
    return {
        "id": report.id,
        "display_no": report.display_no,
        "category": NamedRef(id=category.id, name=category.name),
        "priority": report.priority,
        "status": report.status,
        "building": NamedRef(id=building.id, name=building.name) if building else None,
        "floor": report.floor,
        "detail": report.detail,
        "location_raw": report.location_raw,
        "sla_deadline": report.sla_deadline,
        "sla_status": sla_status_of(report.status, report.created_at, report.sla_deadline, now),
        "created_at": report.created_at,
        "description": report.description,
    }


def cluster_refs(db: Session, report_ids: list[uuid.UUID]) -> dict[uuid.UUID, ReportClusterRef]:
    """신고가 묶인 반복 신고 묶음 (기각된 묶음은 제외, 여러 개면 가장 최근에 감지된 것)."""
    if not report_ids:
        return {}
    rows = db.execute(
        select(
            problem_cluster_reports.c.report_id,
            ProblemCluster.id,
            ProblemCluster.status,
            ProblemCluster.detail,
        )
        .join(ProblemCluster, ProblemCluster.id == problem_cluster_reports.c.cluster_id)
        .where(
            problem_cluster_reports.c.report_id.in_(report_ids),
            ProblemCluster.status != ClusterStatus.REJECTED,
        )
        .order_by(ProblemCluster.detected_at.asc())  # 나중 것이 덮어씀
    ).all()
    chosen = {rid: (cid, status, detail) for rid, cid, status, detail in rows}
    cluster_ids = {cid for cid, _, _ in chosen.values()}
    counts: dict[uuid.UUID, int] = {}
    if cluster_ids:
        for cid, n in db.execute(
            select(problem_cluster_reports.c.cluster_id, func.count())
            .where(problem_cluster_reports.c.cluster_id.in_(cluster_ids))
            .group_by(problem_cluster_reports.c.cluster_id)
        ).all():
            counts[cid] = n
    return {
        rid: ReportClusterRef(
            id=cid,
            report_count=counts.get(cid, 0),
            status=status,
            whole_building=detail == WHOLE_BUILDING,
        )
        for rid, (cid, status, detail) in chosen.items()
    }


def cluster_member_reports(
    db: Session, cluster_id: uuid.UUID, now: datetime | None = None
) -> list[AdminReportItem]:
    """묶음에 묶인 신고 전체 — 접수 순."""
    now = now or datetime.now(UTC)
    rows = db.execute(
        _base()
        .join(problem_cluster_reports, problem_cluster_reports.c.report_id == Report.id)
        .where(problem_cluster_reports.c.cluster_id == cluster_id)
        .order_by(Report.created_at.asc(), Report.display_no.asc())
    ).all()
    refs = cluster_refs(db, [r.id for r, _, _ in rows])
    return [
        AdminReportItem(**_item_fields(r, c, b, now), cluster=refs.get(r.id)) for r, c, b in rows
    ]


def report_summary(db: Session, now: datetime | None = None) -> ReportSummary:
    now = now or datetime.now(UTC)
    by_status = {s.value: 0 for s in ReportStatus}
    for st, n in db.execute(select(Report.status, func.count()).group_by(Report.status)).all():
        by_status[st.value] = n

    def count(*conds: ColumnElement[bool]) -> int:
        return db.scalar(select(func.count()).select_from(Report).where(*conds)) or 0

    return ReportSummary(
        total=sum(by_status.values()),
        by_status=by_status,
        open=sum(by_status[s.value] for s in OPEN_STATUSES),
        sla_over=count(_sla_filter("초과", now)),
        sla_soon=count(_sla_filter("임박", now)),
        urgent_open=count(Report.status.in_(OPEN_STATUSES), Report.priority == Priority.P1),
    )


def list_reports(
    db: Session,
    *,
    status: ReportStatus | None = None,
    category_id: uuid.UUID | None = None,
    priority: Priority | None = None,
    sla_status: SlaStatus | None = None,
    open_only: bool = False,
    sort: SortKey = "-created_at",
    limit: int = 100,
    offset: int = 0,
    now: datetime | None = None,
) -> AdminReportList:
    now = now or datetime.now(UTC)
    conds: list[ColumnElement[bool]] = []
    if status is not None:
        conds.append(Report.status == status)
    if category_id is not None:
        conds.append(Report.category_id == category_id)
    if priority is not None:
        conds.append(Report.priority == priority)
    if sla_status is not None:
        conds.append(_sla_filter(sla_status, now))
    if open_only:
        conds.append(Report.status.in_(OPEN_STATUSES))

    orders: dict[str, list[Any]] = {
        "-created_at": [Report.created_at.desc()],
        "created_at": [Report.created_at.asc()],
        "sla_deadline": [Report.sla_deadline.asc().nulls_last()],  # 마감 급한 순
        "priority": [Report.priority.asc(), Report.created_at.asc()],  # P1부터 (ENUM 선언 순서)
    }
    order = orders[sort]
    rows = db.execute(
        _base().where(*conds).order_by(*order, Report.display_no.desc()).limit(limit).offset(offset)
    ).all()
    total = db.scalar(select(func.count()).select_from(Report).where(*conds)) or 0
    refs = cluster_refs(db, [r.id for r, _, _ in rows])
    items = [AdminReportItem(**_item_fields(r, c, b, now), cluster=refs.get(r.id)) for r, c, b in rows]
    return AdminReportList(items=items, total=total)


def get_report_detail(db: Session, report_id: uuid.UUID, now: datetime | None = None) -> AdminReportDetail | None:
    now = now or datetime.now(UTC)
    row = db.execute(_base().where(Report.id == report_id)).first()
    if row is None:
        return None
    report, category, building = row
    history_rows = db.execute(
        select(ReportStatusHistory, Admin)
        .outerjoin(Admin, Admin.id == ReportStatusHistory.changed_by)
        .where(ReportStatusHistory.report_id == report_id)
        .order_by(ReportStatusHistory.changed_at.asc())
    ).all()
    history = [
        StatusHistoryItem(
            from_status=h.from_status,
            to_status=h.to_status,
            memo=h.memo,
            changed_by=NamedRef(id=a.id, name=a.name) if a else None,
            changed_at=h.changed_at,
        )
        for h, a in history_rows
    ]
    ref = cluster_refs(db, [report.id]).get(report.id)
    return AdminReportDetail(
        **_item_fields(report, category, building, now),
        cluster=ref,
        cluster_reports=cluster_member_reports(db, ref.id, now) if ref else [],
        category_id=report.category_id,
        building_id=report.building_id,
        photo_url=report.photo_url,
        assigned_dept=report.assigned_dept,
        updated_at=report.updated_at,
        status_history=history,
    )
