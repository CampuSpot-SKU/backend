"""로그인 계정 기준 이전 대화 — claim·목록·상세 (작업 1-27, 명세서 5-1, 화면은 1-28).

chat.py(1-26 작업 중)와 같은 /chat 아래지만 파일을 나눠 서로 겹치지 않게 했다.
- 학생 토큰(`typ: "user"`)만 받는다 — 1-24의 get_current_user 재사용(관리자 토큰·없는 토큰은 401).
- 읽기 전용: 이전 대화에 이어 쓰는 API는 없다.
"""
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.models.user import User
from app.routers.user_auth import DbSession, get_current_user
from app.schemas.chat_history import MySessionDetail, MySessionList
from app.services.chat_history import ClaimResult, claim_session, get_my_session, list_my_sessions

router = APIRouter(prefix="/chat", tags=["chat-history"])
CurrentUser = Annotated[User, Depends(get_current_user)]
_UNAUTH: dict[int | str, dict[str, Any]] = {401: {"description": "토큰 없음·만료·위조·관리자 토큰"}}


@router.post(
    "/sessions/{session_id}/claim",
    status_code=204,
    responses={**_UNAUTH, 404: {"description": "세션 없음"}, 409: {"description": "다른 계정이 연결한 세션"}},
)
def claim(session_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Response:
    """지금 대화 세션을 내 계정에 연결 — 이미 내 세션이면 그대로 204."""
    result = claim_session(db, session_id, user.id)
    if result is ClaimResult.NOT_FOUND:
        raise HTTPException(status_code=404, detail="대화 세션을 찾을 수 없어요.")
    if result is ClaimResult.TAKEN:
        raise HTTPException(status_code=409, detail="다른 계정에 연결된 대화예요.")
    return Response(status_code=204)


@router.get("/my-sessions", response_model=MySessionList, responses=_UNAUTH)
def my_sessions(
    user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MySessionList:
    """내 이전 대화 목록 — 최근 대화순, 메시지 없는 세션 제외."""
    return list_my_sessions(db, user.id, limit, offset)


@router.get(
    "/my-sessions/{session_id}",
    response_model=MySessionDetail,
    response_model_exclude_none=True,  # sources는 문의 답변에만
    responses={**_UNAUTH, 404: {"description": "내 계정 세션이 아님(없거나 남의 것)"}},
)
def my_session(session_id: uuid.UUID, user: CurrentUser, db: DbSession) -> MySessionDetail:
    """이전 대화 한 건 — 오래된 순 최대 200개."""
    detail = get_my_session(db, user.id, session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="대화를 찾을 수 없어요.")
    return detail
