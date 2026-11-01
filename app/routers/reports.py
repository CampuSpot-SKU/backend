"""사용자용 신고 상태 조회 — 작업 1-12, 명세서 5-1.

- GET /reports/{display_no}: 본인 확인은 쿼리파라미터 session_id(챗봇이 쓰는 세션) 또는 로그인한 학생 토큰.
  로그인한 학생은 계정에 연결된 대화에서 접수한 신고를 다른 기기에서도 조회할 수 있다(11/1).
- GET /reports/mine: 로그인한 학생 계정의 신고 목록.
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.deps import DbSession
from app.models.user import User
from app.routers.user_auth import _bearer, get_current_user
from app.schemas.reports import MyReport, MyReportList
from app.services.auth import USER_TOKEN_TYPE, AuthNotConfiguredError, decode_access_token
from app.services.my_report import get_my_report, list_my_reports

router = APIRouter(prefix="/reports", tags=["reports"])


def _optional_user_id(
    cred: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> uuid.UUID | None:
    """학생 토큰이 있고 유효하면 계정 id, 없거나 무효면 None (익명 조회는 그대로 session_id로)."""
    if cred is None:
        return None
    try:
        return decode_access_token(cred.credentials, typ=USER_TOKEN_TYPE)
    except AuthNotConfiguredError:
        return None


@router.get("/mine", response_model=MyReportList, responses={401: {"description": "토큰 없음·만료"}})
def my_reports(user: Annotated[User, Depends(get_current_user)], db: DbSession) -> MyReportList:
    """로그인한 학생 계정에 연결된 대화에서 접수한 신고 목록 (최근 것부터)."""
    return MyReportList(reports=list_my_reports(db, user.id))


@router.get(
    "/{display_no}",
    response_model=MyReport,
    responses={404: {"description": "접수번호가 없거나 본인 신고가 아님"}},
)
def get_report_status(
    display_no: int,
    db: DbSession,
    user_id: Annotated[uuid.UUID | None, Depends(_optional_user_id)],
    session_id: uuid.UUID | None = None,
) -> MyReport:
    """본인 신고 처리 상태·이력 조회. 이 session_id로 접수했거나 내 계정에 연결된 대화의 신고만 볼 수 있음."""
    if session_id is None and user_id is None:
        raise HTTPException(status_code=422, detail="session_id 또는 로그인이 필요해요.")
    report = get_my_report(db, display_no, session_id, user_id)
    if report is None:
        raise HTTPException(status_code=404, detail="신고를 찾을 수 없어요.")
    return report
