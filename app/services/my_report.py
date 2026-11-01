"""사용자용 본인 신고 조회 (작업 1-12) — 접수한 세션이거나, 로그인한 학생 계정에 연결된 세션의 신고만 돌려줌.

로그인한 학생은 계정에 연결(claim)된 대화 세션에서 접수한 신고를 다른 기기에서도 조회할 수 있다(11/1 추가).
"""
import uuid

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.orm import Session

from app.models.chat import ChatSession
from app.models.config import Building, Category
from app.models.report import Report, ReportStatusHistory
from app.schemas.admin import NamedRef
from app.schemas.reports import MyReport, MyReportHistoryItem, MyReportSummary

MAX_LIST = 50


def _owner_filter(session_id: uuid.UUID | None, user_id: uuid.UUID | None) -> ColumnElement[bool]:
    conds: list[ColumnElement[bool]] = []
    if session_id is not None:
        conds.append(Report.session_id == session_id)
    if user_id is not None:
        conds.append(
            Report.session_id.in_(select(ChatSession.id).where(ChatSession.user_id == user_id))
        )
    return or_(*conds)


def _location(report: Report, building: Building | None) -> str | None:
    parts = [building.name if building else None]
    if report.floor:
        parts.append(f"{report.floor}층" if report.floor[0].isdigit() else report.floor)
    parts.append(report.detail)
    label = " ".join(p for p in parts if p)
    return label or report.location_raw


def get_my_report(
    db: Session,
    display_no: int,
    session_id: uuid.UUID | None,
    user_id: uuid.UUID | None = None,
) -> MyReport | None:
    """접수번호 + (세션 또는 로그인 계정)이 맞는 신고만 조회. 없거나 남의 신고면 똑같이 None
    (어느 쪽인지 알려주면 접수번호 존재 여부를 추측할 수 있어서)."""
    if session_id is None and user_id is None:
        return None
    row = db.execute(
        select(Report, Category, Building)
        .join(Category, Category.id == Report.category_id)
        .outerjoin(Building, Building.id == Report.building_id)
        .where(Report.display_no == display_no, _owner_filter(session_id, user_id))
    ).first()
    if row is None:
        return None
    report, category, building = row
    history = db.scalars(
        select(ReportStatusHistory)
        .where(ReportStatusHistory.report_id == report.id)
        .order_by(ReportStatusHistory.changed_at.asc())
    ).all()
    return MyReport(
        display_no=report.display_no,
        category=NamedRef(id=category.id, name=category.name),
        priority=report.priority,
        status=report.status,
        created_at=report.created_at,
        location=_location(report, building),
        description=report.description,
        assigned_dept=report.assigned_dept,
        sla_deadline=report.sla_deadline,
        status_history=[
            MyReportHistoryItem(
                from_status=h.from_status, to_status=h.to_status, changed_at=h.changed_at
            )
            for h in history
        ],
    )


def list_my_reports(db: Session, user_id: uuid.UUID) -> list[MyReportSummary]:
    """로그인한 학생 계정에 연결된 세션에서 접수한 신고 목록 (최근 것부터)."""
    rows = db.execute(
        select(Report, Category, Building)
        .join(Category, Category.id == Report.category_id)
        .outerjoin(Building, Building.id == Report.building_id)
        .where(_owner_filter(None, user_id))
        .order_by(Report.created_at.desc())
        .limit(MAX_LIST)
    ).all()
    return [
        MyReportSummary(
            display_no=r.display_no,
            category=NamedRef(id=c.id, name=c.name),
            priority=r.priority,
            status=r.status,
            created_at=r.created_at,
            location=_location(r, b),
        )
        for r, c, b in rows
    ]
