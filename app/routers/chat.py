"""챗봇 대화 — 세션 시작, 메시지 전송(의도분류 → 신고 슬롯필링/접수 또는 행정문의) (작업 1-3).

엔드포인트: 명세서 5-1 "사용자용 — 챗봇"
  POST /chat/sessions                          → {session_id}
  POST /chat/sessions/{session_id}/messages    → 신고 되묻기 / 신고 접수 완료 / 애매함 되묻기 (JSON)
                                                  행정문의는 SSE 스트림
흐름:
  1) 신고 되묻기에 대한 답변이면 → 의도분류 없이 슬롯필링 계속 ('취소'면 중단)
  2) 아니면 ai 서비스로 의도분류 → report / inquiry / unclear
  3) report: 대화에서 슬롯 추출 → 빠진 게 있으면 되묻기, 다 모이면 reports에 접수 생성
판단 로직은 app/services/slot_filling.py, 접수 생성은 app/services/report_service.py.
"""
import json
import logging
import secrets
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.chat import ChatMessage, ChatSession
from app.models.enums import ChatIntent, ChatRole
from app.rate_limit import chat_rate_limit, limiter
from app.schemas.chat import (
    CategoryRef,
    ChatReply,
    MessageIn,
    ReportCreated,
    ReportFollowUp,
    ReportSummary,
    SessionCreated,
    SlotsFilled,
    Unclear,
)
from app.services.ai_client import AiServiceError, HistoryItem, IntentResult, classify_intent
from app.services.report_service import create_report, load_buildings
from app.services.slot_filling import (
    CANCEL_HINT,
    CANCELLED,
    collect_draft,
    extract_slots,
    is_cancel,
    next_question,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])

HISTORY_LOOKBACK = 20  # 진행 중인 신고를 재구성할 때 볼 최근 메시지 수 (되묻기는 최대 2번이라 충분)
DEFAULT_CLARIFY = "무엇에 대해 말씀하시는 건지 조금 더 알려주시겠어요?"
# 행정문의 RAG(작업 1-4)가 붙기 전까지 쓰는 임시 답변 — 1-4에서 ai /rag/answer 스트리밍으로 교체
INQUIRY_PLACEHOLDER = (
    "행정 문의 답변 기능은 아직 준비 중이에요. 곧 학칙·공지를 근거로 답변해 드릴게요!"
)

IntentClassifier = Callable[[str, list[HistoryItem]], IntentResult]


def get_intent_classifier() -> IntentClassifier:
    """테스트에서 app.dependency_overrides로 가짜 분류기로 바꿔 끼우기 위한 의존성."""
    return classify_intent


# 의존성은 Annotated로 선언 (FastAPI 권장 방식, ruff B008 대응)
DbSession = Annotated[Session, Depends(get_db)]
Classifier = Annotated[IntentClassifier, Depends(get_intent_classifier)]


@router.post("/sessions", response_model=SessionCreated, status_code=201)
def create_session(db: DbSession) -> SessionCreated:
    """대화 세션 시작 — 프론트는 받은 session_id를 localStorage에 저장해 이후 요청에 사용."""
    # user_identifier: 익명 식별값 (NOT NULL 컬럼). 본인 확인은 session_id로 하므로 랜덤값이면 충분
    session = ChatSession(user_identifier=secrets.token_urlsafe(16))
    db.add(session)
    db.commit()
    db.refresh(session)
    return SessionCreated(session_id=session.id)


@router.post(
    "/sessions/{session_id}/messages",
    response_model=ChatReply,
    responses={
        200: {
            "description": "신고/애매함은 JSON. 행정문의는 `text/event-stream` — "
            '이벤트마다 `{"delta": "..."}`, 마지막에 `{"done": true, "sources": [...]}`',
            "content": {"text/event-stream": {}},
        },
        404: {"description": "세션 없음 — 프론트는 새 세션을 만들면 됨"},
        429: {"description": "세션당 요청 한도 초과"},
        503: {"description": "ai 서비스 장애 — 잠시 후 다시 시도"},
    },
)
@limiter.limit(chat_rate_limit)
def send_message(
    request: Request,  # slowapi가 요구 (레이트 리밋 키 계산용)
    session_id: uuid.UUID,
    body: MessageIn,
    db: DbSession,
    classify: Classifier,
) -> ChatReply | StreamingResponse:
    if db.get(ChatSession, session_id) is None:
        raise HTTPException(status_code=404, detail="대화 세션을 찾을 수 없어요.")
    text = body.content.strip()
    if not text:
        raise HTTPException(status_code=422, detail="메시지가 비어 있어요.")

    recent = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(HISTORY_LOOKBACK)
    ).all()
    history = list(reversed(recent))
    draft = collect_draft(history)
    user_msg = _add_message(db, session_id, ChatRole.USER, text, _next_ts(history))

    # 1) 신고 되묻기에 대한 답변 → 의도분류 생략하고 슬롯필링 계속
    if draft.in_progress:
        user_msg.intent = ChatIntent.REPORT
        if is_cancel(text):
            _add_message(db, session_id, ChatRole.ASSISTANT, CANCELLED, _after(user_msg))
            db.commit()
            return ReportFollowUp(follow_up_question=CANCELLED, slots_filled=SlotsFilled())
        return _report_step(db, session_id, user_msg, [*draft.user_texts, text],
                            draft.asked, draft.safety_concern)

    # 2) 의도분류 (ai 서비스)
    ai_history = [HistoryItem(role=m.role.value, content=m.content) for m in history]
    try:
        result = classify(text, ai_history)
    except AiServiceError as e:
        # 실패 케이스 재현 로그 (명세서 11장) — 사용자 메시지는 남기고 503
        user_msg.debug_payload = {"error": "ai_unavailable", "detail": str(e)[:500]}
        db.commit()
        raise HTTPException(status_code=503, detail="잠시 후 다시 시도해주세요") from None
    user_msg.intent_scores = {
        "report_score": result.report_score,
        "inquiry_score": result.inquiry_score,
        "safety_concern": result.safety_concern,
    }

    if result.intent == "unclear":
        user_msg.intent = ChatIntent.UNCLEAR
        user_msg.debug_payload = {"ai_response": result.model_dump()}  # 신뢰도 낮은 케이스 기록
        question = result.clarifying_question or DEFAULT_CLARIFY
        reply = _add_message(db, session_id, ChatRole.ASSISTANT, question, _after(user_msg))
        reply.intent = ChatIntent.UNCLEAR
        db.commit()
        return Unclear(clarifying_question=question)

    if result.intent == "inquiry":
        user_msg.intent = ChatIntent.INQUIRY
        reply = _add_message(db, session_id, ChatRole.ASSISTANT, INQUIRY_PLACEHOLDER,
                             _after(user_msg))
        reply.intent = ChatIntent.INQUIRY
        db.commit()
        return StreamingResponse(_sse_once(INQUIRY_PLACEHOLDER), media_type="text/event-stream")

    # 3) 신고 — 새로 시작 (애매함→신고로 이어진 경우 원래 문장도 draft에 포함돼 있음)
    user_msg.intent = ChatIntent.REPORT
    return _report_step(db, session_id, user_msg, [*draft.user_texts, text], draft.asked,
                        draft.safety_concern or result.safety_concern)


def _report_step(
    db: Session,
    session_id: uuid.UUID,
    user_msg: ChatMessage,
    texts: list[str],
    asked: set[str],
    safety_concern: bool,
) -> ReportFollowUp | ReportCreated:
    """슬롯을 뽑아서 되묻거나, 다 모였으면 접수 생성."""
    joined = "\n".join(texts)
    slots = extract_slots(joined, load_buildings(db), safety_concern)
    question = next_question(slots, asked)

    if question is not None:
        content = question + CANCEL_HINT
        reply = _add_message(db, session_id, ChatRole.ASSISTANT, content, _after(user_msg))
        reply.intent = ChatIntent.REPORT  # 신고 흐름 계속 중이라는 표시 (다음 요청에서 재구성용)
        db.commit()
        return ReportFollowUp(
            follow_up_question=content,
            slots_filled=SlotsFilled(
                location=slots.location_text,
                category=slots.category,
                description=joined if slots.has_problem else None,
            ),
        )

    report, category = create_report(db, session_id, slots, description=joined)
    done = (
        f"신고가 접수됐어요! 접수번호는 {report.display_no}번이에요.\n"
        f"· 위치: {slots.location_text or '담당자가 확인할게요'}\n"
        f"· 분류: {category.name} · 우선순위 {report.priority.value}\n"
        "담당 부서에서 확인 후 처리할게요."
    )
    # intent 없이 저장 → 신고 흐름 끝 표시 (다음 메시지는 새 대화로 시작)
    _add_message(db, session_id, ChatRole.ASSISTANT, done, _after(user_msg))
    db.commit()
    logger.info("신고 접수 display_no=%s priority=%s", report.display_no, report.priority.value)
    return ReportCreated(
        report=ReportSummary(
            id=report.id,
            display_no=report.display_no,
            category=CategoryRef(id=category.id, name=category.name),
            priority=report.priority,
            status=report.status,
        )
    )


def _add_message(
    db: Session, session_id: uuid.UUID, role: ChatRole, content: str, created_at: datetime
) -> ChatMessage:
    msg = ChatMessage(session_id=session_id, role=role, content=content, created_at=created_at)
    db.add(msg)
    return msg


# created_at을 파이썬에서 직접 넣는 이유: DB의 now()는 트랜잭션 시작 시각이라, 한 요청에서 저장하는
# 사용자 메시지와 챗봇 답변이 같은 시각이 돼서 순서가 뒤섞일 수 있음 → 항상 앞 메시지보다 뒤로.
def _next_ts(history: list[ChatMessage]) -> datetime:
    now = datetime.now(UTC)
    if history and history[-1].created_at >= now:
        return history[-1].created_at + timedelta(microseconds=1)
    return now


def _after(msg: ChatMessage) -> datetime:
    return max(datetime.now(UTC), msg.created_at + timedelta(microseconds=1))


def _sse_once(text: str) -> Iterator[str]:
    """답변 전체를 delta 한 번 + done 이벤트로 보냄 (1-4에서 진짜 스트리밍으로 교체)."""
    yield f"data: {json.dumps({'delta': text}, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'done': True, 'sources': []})}\n\n"
