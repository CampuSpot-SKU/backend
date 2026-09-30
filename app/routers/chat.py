"""챗봇 대화 — 세션 시작, 메시지 전송(의도분류 → 신고 슬롯필링/접수 또는 행정문의) (작업 1-3).

엔드포인트: 명세서 5-1 "사용자용 — 챗봇"
  POST /chat/sessions                          → {session_id}
  POST /chat/sessions/{session_id}/messages    → 신고 되묻기 / 요약 확인 / 취소 / 접수 완료 / 애매함 되묻기 (JSON)
                                                  행정문의는 SSE 스트림
흐름 (명세서 4-1 "신고 흐름 개편", 작업 1-3c):
  0) 접수 완료 직후의 짧은 인사("네", "고마워요")면 → 의도분류 없이 마무리 한 줄
  1) 신고 흐름 중(접수 제안·되묻기·확인에 대한 답변)이면 → 의도분류 없이 계속
       "취소" → 중단 / "안내만" → 신고 흐름 종료, 행정문의로 답변 / 접수 제안에 "아니요" → 종료
       확인 중 "네/접수" → 접수 / "고칠래요" → 무엇을 고칠지 묻기 / 그 밖의 말 → 정정으로 보고 다시 확인
       (추천 답변 칩은 화면이 글자 그대로 메시지로 보냄 — 직접 입력과 같은 경로)
       말투는 ai가 Gemini로 다듬고, 실패하면 고정 문구. 문구의 종류는 debug_payload.kind에 저장
  2) 아니면 ai 서비스로 의도분류 → report / inquiry / unclear
  3) report: 먼저 "접수를 도와드릴까요?"(추천 답변 칩) → 응 → 빠진 정보를 대화로 묻기 → 문장으로 확인 → 접수
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
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.chat import ChatMessage, ChatSession
from app.models.enums import ChatIntent, ChatRole
from app.rate_limit import chat_rate_limit, limiter
from app.schemas.chat import (
    CategoryRef,
    ChatReply,
    DraftIn,
    MessageIn,
    ReportCancelled,
    ReportConfirm,
    ReportCreated,
    ReportFollowUp,
    ReportSummary,
    SessionCreated,
    SlotsFilled,
    Unclear,
)
from app.services.ai_client import (
    AiServiceError,
    HistoryItem,
    IntentResult,
    classify_intent,
    say_text,
)
from app.services.report_service import create_report, load_buildings
from app.services.slot_filling import (
    ASK_EDIT,
    CANCELLED,
    DECLINED,
    DONE_PREFIX,
    INTRO,
    KIND_EDIT,
    KIND_OFFER,
    KIND_SUMMARY,
    OFFER_CHOICES,
    RESET_NOTE,
    SUMMARY_CHOICES,
    THANKS_REPLY,
    Draft,
    ReportSlots,
    apply_form,
    build_offer,
    build_summary,
    collect_draft,
    extract_slots,
    is_cancel,
    is_confirm,
    is_edit,
    is_no,
    is_short_thanks,
    is_yes,
    judge_reason,
    next_question,
    wants_inquiry,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])

HISTORY_LOOKBACK = 30  # 진행 중인 신고를 재구성할 때 볼 최근 메시지 수 (되묻기·요약·정정 몇 번을 포함해도 충분)
DEFAULT_CLARIFY = "무엇에 대해 말씀하시는 건지 조금 더 알려주시겠어요?"
# 행정문의 RAG(작업 1-4)가 붙기 전까지 쓰는 임시 답변 — 1-4에서 ai /rag/answer 스트리밍으로 교체
INQUIRY_PLACEHOLDER = (
    "행정 문의 답변 기능은 아직 준비 중이에요. 곧 학칙·공지를 근거로 답변해 드릴게요!"
)

IntentClassifier = Callable[[str, list[HistoryItem]], IntentResult]
# (종류, 기본 문구, 남아 있어야 할 표현, 최근 대화) → 말투를 다듬은 문구. 실패하면 기본 문구
Phraser = Callable[[str, str, list[str], list[HistoryItem]], str]
Phrase = Callable[[str, str, list[str]], str]  # 대화 맥락을 미리 채운 형태


def get_intent_classifier() -> IntentClassifier:
    """테스트에서 app.dependency_overrides로 가짜 분류기로 바꿔 끼우기 위한 의존성."""
    return classify_intent


def get_phraser() -> Phraser:
    """테스트에서 고정 문구(또는 가짜)로 바꿔 끼우기 위한 의존성."""
    return say_text


# 의존성은 Annotated로 선언 (FastAPI 권장 방식, ruff B008 대응)
DbSession = Annotated[Session, Depends(get_db)]
Classifier = Annotated[IntentClassifier, Depends(get_intent_classifier)]
PhraserDep = Annotated[Phraser, Depends(get_phraser)]


@router.post("/sessions", response_model=SessionCreated, status_code=201)
def create_session(db: DbSession) -> SessionCreated:
    """대화 세션 시작 — 프론트는 받은 session_id를 localStorage에 저장해 이후 요청에 사용."""
    # user_identifier: 익명 식별값 (NOT NULL 컬럼). 본인 확인은 session_id로 하므로 랜덤값이면 충분
    session = ChatSession(user_identifier=secrets.token_urlsafe(16))
    db.add(session)
    db.commit()
    db.refresh(session)
    return SessionCreated(session_id=session.id)


def _recent_history(db: Session, session_id: uuid.UUID) -> list[ChatMessage]:
    recent = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(HISTORY_LOOKBACK)
    ).all()
    return list(reversed(recent))


@router.post(
    "/sessions/{session_id}/reset",
    status_code=204,
    responses={404: {"description": "세션 없음"}, 429: {"description": "세션당 요청 한도 초과"}},
)
@limiter.limit(chat_rate_limit)
def reset_flow(
    request: Request,  # slowapi가 요구 (레이트 리미팅 키 계산용)
    session_id: uuid.UUID,
    db: DbSession,
) -> Response:
    """진행 중이던 신고 흐름을 끝냄 — 프론트가 페이지를 새로 열 때(새로고침) 한 번 호출.

    화면은 새로고침하면 대화 기록 없이 인사말만 보이는데 서버는 같은 session_id의 이전 대화를 기억하고
    있어서, 끝나지 않은 신고에 새 입력이 "정정"으로 이어붙는 문제를 막는다. 세션은 그대로 둠
    (본인 신고 조회가 session_id로 본인 확인을 하므로). 진행 중인 신고가 없으면 아무 일도 안 함.
    """
    if db.get(ChatSession, session_id) is None:
        raise HTTPException(status_code=404, detail="대화 세션을 찾을 수 없어요.")
    history = _recent_history(db, session_id)
    if collect_draft(history).in_progress:
        # intent 없는 챗봇 메시지 → collect_draft가 신고 흐름의 끝으로 봄
        _add_message(db, session_id, ChatRole.ASSISTANT, RESET_NOTE, _next_ts(history))
        db.commit()
    return Response(status_code=204)


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
    phraser: PhraserDep,
) -> ChatReply | StreamingResponse:
    if db.get(ChatSession, session_id) is None:
        raise HTTPException(status_code=404, detail="대화 세션을 찾을 수 없어요.")
    text = body.content.strip()
    if not text:
        raise HTTPException(status_code=422, detail="메시지가 비어 있어요.")

    history = _recent_history(db, session_id)
    draft = collect_draft(history)
    user_msg = _add_message(db, session_id, ChatRole.USER, text, _next_ts(history))
    action = body.action
    say_history = [HistoryItem(role=m.role.value, content=m.content) for m in history[-3:]]
    say_history.append(HistoryItem(role="user", content=text))

    def phrase(kind: str, base: str, must: list[str]) -> str:
        return phraser(kind, base, must, say_history)

    # 0) 접수 완료 직후의 인사·확인성 짧은 답 → 새 신고로 시작하지 않고 마무리 한 줄 (명세 4-1 보완 ②)
    if (
        not draft.in_progress
        and action is None
        and _just_reported(history)
        and is_short_thanks(text)
    ):
        _add_message(db, session_id, ChatRole.ASSISTANT, THANKS_REPLY, _after(user_msg))
        db.commit()
        return StreamingResponse(_sse_once(THANKS_REPLY), media_type="text/event-stream")

    # 신고 흐름에서 빠져나와 안내만 받기 — 버튼 또는 글자
    if action == "switch_to_inquiry" or (draft.in_progress and wants_inquiry(text)):
        return _switch_to_inquiry(db, session_id, user_msg, draft)

    # 1) 신고 흐름 중(되묻기·요약 확인에 대한 답변) → 의도분류 생략하고 슬롯필링 계속
    if draft.in_progress or action in ("confirm_report", "cancel_report"):
        user_msg.intent = ChatIntent.REPORT
        if action == "cancel_report" or (action is None and is_cancel(text)):
            _add_message(db, session_id, ChatRole.ASSISTANT, CANCELLED, _after(user_msg))
            db.commit()  # intent 없이 저장 → 신고 흐름 끝 표시
            return ReportCancelled(
                message=CANCELLED, follow_up_question=CANCELLED, slots_filled=SlotsFilled()
            )
        if draft.in_progress and action is None and draft.last_kind == KIND_OFFER:
            # 접수 제안에 대한 답 — 거절이면 끝, 긍정이면 필요한 정보 묻기, 그 밖의 말은 추가 정보로 보고 이어감
            if is_no(text):
                _add_message(db, session_id, ChatRole.ASSISTANT, DECLINED, _after(user_msg))
                db.commit()  # intent 없이 저장 → 신고 흐름 끝 표시
                return ReportCancelled(
                    message=DECLINED, follow_up_question=DECLINED, slots_filled=SlotsFilled()
                )
            if is_yes(text):
                texts, descs = list(draft.user_texts), list(draft.desc_texts)
            else:
                texts, descs = draft.with_reply(text)
            return _report_step(
                db, session_id, user_msg, texts, descs, draft.asked, draft.safety_concern,
                phrase, intro=True,
            )
        if draft.confirming and action is None and is_edit(text) and not is_confirm(text):
            return _ask_edit(db, session_id, user_msg, draft, phrase)
        confirm = draft.in_progress and (
            action == "confirm_report" or (draft.confirming and action is None and is_confirm(text))
        )
        if action == "confirm_report" and not draft.in_progress:
            # 화면이 오래돼 서버엔 진행 중인 신고가 없음 → 폼에 적힌 내용으로 새로 시작해 요약부터
            texts = [t for t in [(body.draft.description if body.draft else None)] if t]
            return _report_step(db, session_id, user_msg, texts, texts, set(), False, phrase)
        if confirm:
            texts, descs = list(draft.user_texts), list(draft.desc_texts)
        else:
            texts, descs = draft.with_reply(text)  # 위치 답변·질문은 상황에서 빼는 규칙 적용
        return _report_step(
            db, session_id, user_msg, texts, descs, draft.asked, draft.safety_concern, phrase,
            confirm=confirm, form=body.draft if confirm else None,
        )

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

    # 3) 신고 — 먼저 접수를 도와드릴지 물음 (애매함→신고로 이어진 경우 원래 문장도 draft에 포함돼 있음)
    user_msg.intent = ChatIntent.REPORT
    return _offer(
        db, session_id, user_msg, [*draft.user_texts, text], [*draft.desc_texts, text],
        draft.safety_concern or result.safety_concern, phrase,
    )


def _just_reported(history: list[ChatMessage]) -> bool:
    """직전 챗봇 메시지가 접수 완료 안내였나."""
    return bool(history) and (
        history[-1].role == ChatRole.ASSISTANT
        and history[-1].intent is None
        and history[-1].content.startswith(DONE_PREFIX)
    )


def _switch_to_inquiry(
    db: Session, session_id: uuid.UUID, user_msg: ChatMessage, draft: Draft
) -> StreamingResponse:
    """[안내만 받을래요] — 신고 흐름을 끝내고 첫 발화를 행정문의로 답변.

    intent=행정문의로 저장하면 collect_draft가 신고 흐름 끝으로 봄.
    TODO(1-4): 행정문의 RAG가 붙으면 첫 신고 문장(`draft.user_texts[0]`, 없으면 이번 메시지)으로
    답변을 만들 것 — 지금은 임시 문구.
    """
    user_msg.intent = ChatIntent.INQUIRY
    reply = _add_message(db, session_id, ChatRole.ASSISTANT, INQUIRY_PLACEHOLDER, _after(user_msg))
    reply.intent = ChatIntent.INQUIRY
    db.commit()
    return StreamingResponse(_sse_once(INQUIRY_PLACEHOLDER), media_type="text/event-stream")


def _slots_filled(slots: ReportSlots, description: str | None) -> SlotsFilled:
    return SlotsFilled(
        location=slots.location_text,
        category=slots.category,
        description=description if slots.has_problem else None,
        building=slots.building,
        floor=slots.floor,
        detail=slots.detail,
    )


def _offer(
    db: Session,
    session_id: uuid.UUID,
    user_msg: ChatMessage,
    texts: list[str],
    descs: list[str],
    safety_concern: bool,
    phrase: Phrase,
) -> ReportFollowUp:
    """"시설물 신고 접수에 관한 내용 같아요. 접수를 도와드릴까요?" — 바로 묻지 않고 먼저 의사를 확인."""
    slots = extract_slots("\n".join(texts), load_buildings(db), safety_concern)
    content = phrase(KIND_OFFER, build_offer(" ".join(descs)), ["접수"])
    reply = _add_message(db, session_id, ChatRole.ASSISTANT, content, _after(user_msg), KIND_OFFER)
    reply.intent = ChatIntent.REPORT
    db.commit()
    return ReportFollowUp(
        follow_up_question=content,
        slots_filled=_slots_filled(slots, "\n".join(descs)),
        choices=list(OFFER_CHOICES),
    )


def _ask_edit(
    db: Session, session_id: uuid.UUID, user_msg: ChatMessage, draft: Draft, phrase: Phrase
) -> ReportFollowUp:
    """"내용을 고칠래요" — 무엇을 고칠지 묻고, 다음 말을 정정으로 받음."""
    content = phrase(KIND_EDIT, ASK_EDIT, [])
    reply = _add_message(db, session_id, ChatRole.ASSISTANT, content, _after(user_msg), KIND_EDIT)
    reply.intent = ChatIntent.REPORT
    db.commit()
    slots = extract_slots(draft.text, [], draft.safety_concern)
    return ReportFollowUp(
        follow_up_question=content,
        slots_filled=_slots_filled(slots, "\n".join(draft.desc_texts)),
    )


def _report_step(
    db: Session,
    session_id: uuid.UUID,
    user_msg: ChatMessage,
    texts: list[str],  # 슬롯(위치·상황) 추출에 쓸 메시지들
    descs: list[str],  # 접수 상황(description)으로 쓸 메시지들
    asked: set[str],
    safety_concern: bool,
    phrase: Phrase,
    *,
    intro: bool = False,  # 접수 제안에 "응"이라고 답한 직후 — "필요한 정보를 물어볼게요."로 시작
    confirm: bool = False,
    form: DraftIn | None = None,
) -> ReportFollowUp | ReportConfirm | ReportCreated:
    """슬롯을 뽑아서 되묻거나(빠진 게 있음), 문장으로 확인하거나(다 모임), 확인 답이면 접수 생성."""
    joined = "\n".join(texts)
    described = "\n".join(descs)
    buildings = load_buildings(db)
    form_desc = (form.description or "").strip() if form else ""
    # 폼에서 상황 설명을 고쳤으면 그 내용도 판정에 반영
    judged = f"{joined}\n{form_desc}" if form_desc and form_desc != described else joined
    slots = extract_slots(judged, buildings, safety_concern)
    if confirm:
        if form is not None:
            slots = apply_form(slots, form.building, form.floor, form.detail, buildings, judged)
        return _create(db, session_id, user_msg, slots, form_desc or described or joined)

    question = next_question(slots, asked)
    if question is not None:
        base = (INTRO if intro else "") + question.text
        content = phrase(question.key, base, question.must_include)
        kind = question.key  # ASK_LOC / ASK_PROB 값이 곧 저장하는 메시지 종류
        reply = _add_message(db, session_id, ChatRole.ASSISTANT, content, _after(user_msg), kind)
        reply.intent = ChatIntent.REPORT  # 신고 흐름 계속 중이라는 표시 (다음 요청에서 재구성용)
        db.commit()
        return ReportFollowUp(
            follow_up_question=content,
            slots_filled=_slots_filled(slots, described),
            choices=question.choices,
        )

    # 필수 항목이 찼거나 같은 질문을 이미 했음 → 바로 접수하지 않고 문장으로 확인
    base = build_summary(slots, descs)
    must = [t for t in [slots.location_text] if t and t in base] + ["접수"]
    summary = phrase(KIND_SUMMARY, base, must)
    reply = _add_message(db, session_id, ChatRole.ASSISTANT, summary, _after(user_msg), KIND_SUMMARY)
    reply.intent = ChatIntent.REPORT
    db.commit()
    return ReportConfirm(
        summary=summary,
        follow_up_question=summary,
        slots_filled=_slots_filled(slots, described),
        choices=list(SUMMARY_CHOICES),
    )


def _create(
    db: Session,
    session_id: uuid.UUID,
    user_msg: ChatMessage,
    slots: ReportSlots,
    description: str,
) -> ReportCreated:
    report, category = create_report(db, session_id, slots, description=description)
    where = f"{slots.location_text}에서 생긴 " if slots.location_text else ""
    done = (
        f"{DONE_PREFIX}! 접수번호는 {report.display_no}번이에요. "
        f"{where}{category.name} 문제로 분류했어요. "
        f"{judge_reason(slots, report.priority.value)} 담당 부서에서 확인 후 처리할게요."
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
        ),
        message=done,
    )


def _add_message(
    db: Session,
    session_id: uuid.UUID,
    role: ChatRole,
    content: str,
    created_at: datetime,
    kind: str | None = None,  # 챗봇 메시지 종류 — 말투가 바뀌어도 대화 재구성에서 알아보게 저장
) -> ChatMessage:
    msg = ChatMessage(session_id=session_id, role=role, content=content, created_at=created_at)
    if kind:
        msg.debug_payload = {"kind": kind}
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
