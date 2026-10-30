"""상세보기 신고 대화 열람 — 신고가 접수되기까지 학생과 챗봇이 나눈 대화 (작업 1-25, 명세서 4-2·5-1).

- 대상: 신고의 세션(reports.session_id) 대화 중 **그 신고의 신고 흐름 구간**만.
  구간의 끝 = 신고가 만들어진 직후 같은 요청에서 저장되는 "접수 완료 안내"(챗봇 메시지, intent 없음 —
  chat.py `_create`가 "신고 흐름 끝 표시"로 intent 없이 저장). 거기서부터 거슬러 올라가며 intent=신고가
  이어지는 메시지까지 (slot_filling.collect_draft와 같은 구간 규칙).
- 같은 세션의 문의 대화, 앞서 접수한 다른 신고의 구간은 섞지 않는다 (앞 구간은 자기 접수 완료 안내로 끊김).
- 내보내는 것은 role·content·created_at뿐 — session_id·user_identifier·intent_scores·debug_payload는 제외
  (익명 원칙, 명세 5-1·11장). content는 저장 전에 이미 학번·전화번호가 마스킹돼 있다(1-23).
- 세션이 없거나(데모 데이터 신고·세션 정리) 접수 완료 안내를 못 찾으면 빈 목록.
"""
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chat import ChatMessage
from app.models.enums import ChatIntent, ChatRole
from app.models.report import Report
from app.schemas.admin import ConversationMessage, ReportConversation

# 한 번에 보여 주는 최대 메시지 수 — 넘으면 최근 쪽을 남기고 truncated=true
MAX_MESSAGES = 80
# 신고 시각(DB now())과 메시지 시각(서버 파이썬 시계)이 어긋날 수 있어 접수 완료 안내를 찾을 때 두는 여유
CLOCK_SKEW = timedelta(seconds=5)
# 접수 완료 안내는 신고 직후 저장되지만, 세션 전체를 읽지 않도록 신고 시각 뒤 이 시간까지만 가져옴
FETCH_AFTER = timedelta(minutes=2)


class MessageLike(Protocol):
    role: ChatRole
    content: str
    intent: ChatIntent | None
    created_at: datetime


M = TypeVar("M", bound=MessageLike)


def pick_flow(messages: Sequence[M], report_created_at: datetime) -> tuple[list[M], bool]:
    """세션 메시지(오래된 순)에서 이 신고의 신고 흐름 구간을 고른다 → (메시지들, 잘렸는지).

    끝 = 신고 시각 − 여유 이후 처음 나오는 intent 없는 챗봇 메시지(접수 완료 안내).
    시작 = 끝에서 거슬러 올라가며 intent=신고가 이어지는 첫 메시지. 못 찾으면 ([], False).
    """
    lo = report_created_at - CLOCK_SKEW
    end = next(
        (
            i
            for i, m in enumerate(messages)
            if m.role == ChatRole.ASSISTANT and m.intent is None and m.created_at >= lo
        ),
        None,
    )
    if end is None:
        return [], False
    start = end - 1
    while start >= 0 and messages[start].intent == ChatIntent.REPORT:
        start -= 1
    flow = list(messages[start + 1 : end + 1])
    if len(flow) > MAX_MESSAGES:
        return flow[-MAX_MESSAGES:], True
    return flow, False


def get_report_conversation(db: Session, report_id: uuid.UUID) -> ReportConversation | None:
    """None = 신고 없음(404). 대화가 없으면 messages=[]."""
    row = db.execute(
        select(Report.display_no, Report.session_id, Report.created_at).where(Report.id == report_id)
    ).one_or_none()
    if row is None:
        return None
    display_no, session_id, created_at = row
    if session_id is None:
        return ReportConversation(report_id=report_id, display_no=display_no, messages=[], truncated=False)

    messages = list(
        db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id, ChatMessage.created_at <= created_at + FETCH_AFTER)
            .order_by(ChatMessage.created_at)
        ).scalars()
    )
    flow, truncated = pick_flow(messages, created_at)
    return ReportConversation(
        report_id=report_id,
        display_no=display_no,
        messages=[ConversationMessage(role=m.role, content=m.content, created_at=m.created_at) for m in flow],
        truncated=truncated,
    )
