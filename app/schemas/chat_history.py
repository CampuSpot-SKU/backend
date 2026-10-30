"""이전 대화 조회 API 응답 형식 — 명세서 5-1 "사용자용 — 이전 대화" (작업 1-27, 화면은 1-28).

읽기 전용. 세션의 user_identifier, 메시지의 intent·intent_scores·debug_payload는 내보내지 않는다.
"""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.models.enums import ChatRole


class MySessionItem(BaseModel):
    session_id: uuid.UUID
    title: str  # 첫 학생 메시지 앞 30자 (사진만이면 "사진 대화")
    started_at: datetime
    last_message_at: datetime
    message_count: int


class MySessionList(BaseModel):
    total: int
    items: list[MySessionItem]


class HistoryMessage(BaseModel):
    role: ChatRole  # user = 학생, assistant = 챗봇
    content: str
    created_at: datetime
    sources: list[dict[str, Any]] | None = None  # 문의 답변에만 — 없으면 응답에서 필드 생략


class MySessionDetail(BaseModel):
    session_id: uuid.UUID
    started_at: datetime
    messages: list[HistoryMessage]
    truncated: bool  # 메시지가 MAX_MESSAGES를 넘어 가장 최근 것만 보냄
