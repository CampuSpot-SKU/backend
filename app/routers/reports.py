"""사용자용 신고 상태 조회 (GET /reports/{display_no}) — 작업 1-12, 명세서 5-1.

로그인이 없는 익명 서비스라 본인 확인은 쿼리파라미터 session_id(챗봇이 쓰는 세션)로 한다.
"""
import uuid

from fastapi import APIRouter, HTTPException

from app.deps import DbSession
from app.schemas.reports import MyReport
from app.services.my_report import get_my_report

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get(
    "/{display_no}",
    response_model=MyReport,
    responses={404: {"description": "접수번호가 없거나 이 세션에서 접수한 신고가 아님"}},
)
def get_report_status(display_no: int, session_id: uuid.UUID, db: DbSession) -> MyReport:
    """본인 신고 처리 상태·이력 조회. 이 session_id로 접수한 신고만 볼 수 있음."""
    report = get_my_report(db, display_no, session_id)
    if report is None:
        raise HTTPException(status_code=404, detail="신고를 찾을 수 없어요.")
    return report
