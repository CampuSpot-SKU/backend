"""관리자 행정 문의 로그 — 학생 질문·챗봇 답변·근거 목록 (작업 1-18, 명세서 4-2·5-1).

인증은 다른 관리자 API와 같음(Authorization: Bearer, CurrentAdmin). 읽기 전용.
묶는 방식·신뢰도 낮음 기준은 services/chat_log_query.py 맨 위 주석.
"""
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.deps import CurrentAdmin, DbSession
from app.models.enums import ChatIntent
from app.schemas.chat_logs import ChatLogList
from app.services.chat_log_query import list_chat_logs

router = APIRouter(prefix="/admin", tags=["admin-chat-logs"])

_AUTH_ERRORS: dict[int | str, dict[str, Any]] = {401: {"description": "토큰 없음·만료·위조"}, 503: {"description": "JWT_SECRET 미설정"}}


@router.get("/chat-logs", response_model=ChatLogList, responses=_AUTH_ERRORS)
def get_chat_logs(
    db: DbSession,
    _admin: CurrentAdmin,
    days: Annotated[int | None, Query(ge=1, le=365, description="최근 N일 질문만. 없으면 전체 기간")] = None,
    intent: Annotated[ChatIntent, Query(description="질문의 의도 (기본 문의)")] = ChatIntent.INQUIRY,
    low_confidence: Annotated[bool, Query(description="true면 의도 신뢰도가 낮거나 오류·되묻기 기록이 있는 질문만")] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ChatLogList:
    """학생 질문과 바로 뒤 챗봇 답변을 한 줄로 — 최신순."""
    return list_chat_logs(db, intent=intent, days=days, low_confidence=low_confidence, limit=limit, offset=offset)
