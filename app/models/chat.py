"""챗봇 대화 세션/메시지."""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models._common import created_at, pg_enum, uuid_pk
from app.models.enums import ChatIntent, ChatRole


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    # 익명 세션쿠키 값 — "내 신고 조회" 본인 확인용
    user_identifier: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    started_at: Mapped[datetime] = created_at()


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_session_created", "session_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[ChatRole] = mapped_column(pg_enum(ChatRole, "chat_role"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[ChatIntent | None] = mapped_column(pg_enum(ChatIntent, "chat_intent"))
    # 예: {"report_score": 95, "inquiry_score": 5}
    intent_scores: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # 신뢰도 낮거나 예외 발생 시에만 원본 프롬프트/응답 저장, 평소엔 null (명세서 11장)
    debug_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # 행정 문의 답변의 근거 [{"title","article_no","url"}] — 문의 로그(1-18)와 SSE done 이벤트가 같은 형식 (1-4c, 명세 5-1)
    sources: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at()
