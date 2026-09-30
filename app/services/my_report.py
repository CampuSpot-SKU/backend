"""사용자용 본인 신고 조회 (작업 1-12) — session_id가 접수한 세션과 같을 때만 돌려줌."""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.config import Category
from app.models.report import Report, ReportStatusHistory
from app.schemas.admin import NamedRef
from app.schemas.reports import MyReport, MyReportHistoryItem


def get_my_report(db: Session, display_no: int, session_id: uuid.UUID) -> MyReport | None:
    """접수번호 + 세션이 모두 맞는 신고만 조회. 없거나 남의 신고면 똑같이 None
    (어느 쪽인지 알려주면 접수번호 존재 여부를 추측할 수 있어서)."""
    row = db.execute(
        select(Report, Category)
        .join(Category, Category.id == Report.category_id)
        .where(Report.display_no == display_no, Report.session_id == session_id)
    ).first()
    if row is None:
        return None
    report, category = row
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
        status_history=[
            MyReportHistoryItem(
                from_status=h.from_status, to_status=h.to_status, changed_at=h.changed_at
            )
            for h in history
        ],
    )
