"""챗봇 API 요청/응답 형식 — 명세서 5-1 "사용자용 — 챗봇" 그대로.

필드명을 바꾸면 frontend(src/api/chat.ts)와 계약이 깨지므로, 바꿔야 하면 명세서부터 수정할 것.
"""
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.models.enums import Priority, ReportStatus


class SessionCreated(BaseModel):
    session_id: uuid.UUID


class MessageIn(BaseModel):
    # ai 의도분류 API의 입력 제한(1000자)과 맞춤
    content: str = Field(min_length=1, max_length=1000, examples=["3동 2층 화장실 물이 계속 새요"])


class SlotsFilled(BaseModel):
    """슬롯필링 진행 상황 — 지금까지 대화에서 알아낸 값 (모르면 null)."""

    location: str | None = Field(default=None, examples=["3동 2층 화장실"])
    category: str | None = Field(default=None, examples=["시설·설비"])
    description: str | None = Field(default=None, examples=["물이 계속 새요"])


class ReportFollowUp(BaseModel):
    """신고 접수 중 — 빠진 정보를 되묻는 중."""

    intent: Literal["report"] = "report"
    follow_up_question: str
    slots_filled: SlotsFilled


class CategoryRef(BaseModel):
    id: uuid.UUID
    name: str


class ReportSummary(BaseModel):
    id: uuid.UUID
    display_no: int
    category: CategoryRef
    priority: Priority
    status: ReportStatus


class ReportCreated(BaseModel):
    """신고 접수 완료."""

    intent: Literal["report"] = "report"
    report_created: Literal[True] = True
    report: ReportSummary


class Unclear(BaseModel):
    """신고인지 문의인지 애매 — 되묻기."""

    intent: Literal["unclear"] = "unclear"
    clarifying_question: str


# 행정문의("inquiry")는 JSON이 아니라 SSE 스트림(text/event-stream)으로 응답하므로 여기 없음.
ChatReply = ReportFollowUp | ReportCreated | Unclear
