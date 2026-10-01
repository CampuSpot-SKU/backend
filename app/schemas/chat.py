"""챗봇 API 요청/응답 형식 — 명세서 5-1 "사용자용 — 챗봇" 그대로.

필드명을 바꾸면 frontend(src/api/chat.ts)와 계약이 깨지므로, 바꿔야 하면 명세서부터 수정할 것.
"""
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.models.enums import Priority, ReportStatus


class SessionCreated(BaseModel):
    session_id: uuid.UUID


ChatAction = Literal["switch_to_inquiry", "confirm_report", "cancel_report"]


class DraftIn(BaseModel):
    """접수 폼의 값 — [접수]를 누를 때 최종 값으로 보냄 (B단계 1-3d부터는 채팅마다). 전부 선택."""

    building: str | None = Field(default=None, max_length=100)
    floor: str | None = Field(default=None, max_length=20)
    detail: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2000)


class MessageIn(BaseModel):
    # ai 의도분류 API의 입력 제한(1000자)과 맞춤
    content: str = Field(min_length=1, max_length=1000, examples=["3동 2층 화장실 물이 계속 새요"])
    # 버튼([안내만 받을래요]/[접수]/[취소])을 눌렀을 때만 — 이때 content는 버튼 글자
    action: ChatAction | None = None
    draft: DraftIn | None = None


class SlotsFilled(BaseModel):
    """슬롯필링 진행 상황 — 지금까지 대화에서 알아낸 값 (모르면 null). 화면의 접수 폼에 실시간 반영됨."""

    location: str | None = Field(default=None, examples=["3동 2층 화장실"])
    category: str | None = Field(default=None, examples=["시설·설비"])
    description: str | None = Field(default=None, examples=["물이 계속 새요"])
    building: str | None = Field(default=None, examples=["은주1관"])
    floor: str | None = Field(default=None, examples=["2", "B1"])
    detail: str | None = Field(default=None, examples=["화장실"])


class ReportFollowUp(BaseModel):
    """신고 접수 중 — 빠진 정보를 되묻는 중."""

    intent: Literal["report"] = "report"
    follow_up_question: str
    slots_filled: SlotsFilled
    # 눌러서 고를 수 있는 추천 답변 (접수 제안: ["네, 접수해 주세요", "아니요, 안내만 받을게요"] 등).
    # 누르면 그 글자를 일반 메시지로 보내면 됨 (직접 입력도 항상 가능). 없으면 null
    choices: list[str] | None = None


class ReportConfirm(BaseModel):
    """요약 확인 단계 — [접수] [수정] [취소]를 보여줌."""

    intent: Literal["report"] = "report"
    confirm_required: Literal[True] = True
    summary: str
    follow_up_question: str  # summary와 같은 문구 (1-5b 이전 화면이 글자로 보여줄 수 있게 하는 하위 호환)
    slots_filled: SlotsFilled
    # 눌러서 고를 수 있는 추천 답변 ("네, 접수해 주세요" 등) — 직접 입력해도 됨
    choices: list[str] | None = None


class ReportCancelled(BaseModel):
    """신고 취소됨."""

    intent: Literal["report"] = "report"
    report_cancelled: Literal[True] = True
    message: str
    follow_up_question: str  # message와 같은 문구 (하위 호환)
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
    # backend가 만든 완성 안내 문구 (접수번호·위치·판정 이유) — chat_messages에 저장되는 문구와 같음
    message: str
    # 이어서 접수할 남은 건 (한 번에 한 건씩 접수한 경우) — 누르면 그 글자가 새 신고 메시지로 전송됨. 없으면 null
    choices: list[str] | None = None


class Unclear(BaseModel):
    """신고인지 문의인지 애매 — 되묻기."""

    intent: Literal["unclear"] = "unclear"
    clarifying_question: str


# 행정문의("inquiry")는 JSON이 아니라 SSE 스트림(text/event-stream)으로 응답하므로 여기 없음.
ChatReply = ReportFollowUp | ReportConfirm | ReportCancelled | ReportCreated | Unclear
