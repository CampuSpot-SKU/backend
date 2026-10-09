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
from app.services import campus_places as cp
from app.services import report_agent as ra
from app.services.ai_client import (
    AgentState,
    AiServiceError,
    HistoryItem,
    IntentResult,
    JudgeResult,
    PhotoData,
    RagAnswer,
    classify_intent,
    judge_report,
    rag_answer,
    report_turn,
    say_text,
)
from app.services.photo_storage import PhotoStorage, StorageError, get_photo_storage
from app.services.pii_mask import mask_optional, mask_pii
from app.services.report_service import create_report, load_buildings, load_category_names
from app.services.slot_filling import (
    ASK_EDIT,
    CANCELLED,
    DECLINED,
    DONE_PREFIX,
    INTRO,
    KIND_EDIT,
    KIND_OFFER,
    KIND_SUMMARY,
    NON_REPORT_KIND,
    OFFER_CHOICES,
    RESET_NOTE,
    SUMMARY_CHOICES,
    THANKS_REPLY,
    UNKNOWN_WORDS,
    BuildingRef,
    Draft,
    ReportSlots,
    apply_form,
    apply_judgement,
    build_offer,
    build_summary,
    check_not_report,
    collect_draft,
    extract_slots,
    find_ambiguity,
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
from app.services.smalltalk import pick_reply

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])

HISTORY_LOOKBACK = 30  # 진행 중인 신고를 재구성할 때 볼 최근 메시지 수 (되묻기·요약·정정 몇 번을 포함해도 충분)
DEFAULT_CLARIFY = "무엇에 대해 말씀하시는 건지 조금 더 알려주시겠어요?"
IntentClassifier = Callable[[str, list[HistoryItem]], IntentResult]
# (종류, 기본 문구, 남아 있어야 할 표현, 최근 대화) → 말투를 다듬은 문구. 실패하면 기본 문구
Phraser = Callable[[str, str, list[str], list[HistoryItem]], str]
# (신고 내용, 확인된 위치, 카테고리 이름들) → AI 판정 (1-3b). 실패하면 AiServiceError
Judger = Callable[[str, str | None, list[str], PhotoData | None], JudgeResult]
Phrase = Callable[[str, str, list[str]], str]
Agent = ra.Agent  # 대화 맥락을 미리 채운 형태
# 질문 → 근거 기반 답변 + sources (1-4c). 실패하면 AiServiceError
Answerer = Callable[[str], RagAnswer]
Judge = Callable[[ReportSlots, str], ReportSlots]  # 규칙으로 뽑은 슬롯 → AI 판정을 반영한 슬롯
# 접수된 신고 id → 대기 사진을 옮긴 저장소 경로(사진 없음·저장소 실패면 None) — 작업 1-10
AttachPhoto = Callable[[uuid.UUID], str | None]
PHOTO_SENT = " 사진도 함께 전달했어요."  # 사진이 붙은 접수만 완료 문구 끝에 붙임 (명세 5-1)


def get_intent_classifier() -> IntentClassifier:
    """테스트에서 app.dependency_overrides로 가짜 분류기로 바꿔 끼우기 위한 의존성."""
    return classify_intent


def get_judger() -> Judger:
    """테스트에서 app.dependency_overrides로 가짜 판정기로 바꿔 끼우기 위한 의존성."""
    return judge_report


def get_phraser() -> Phraser:
    """테스트에서 고정 문구(또는 가짜)로 바꿔 끼우기 위한 의존성."""
    return say_text


def get_answerer() -> Answerer:
    """테스트에서 app.dependency_overrides로 가짜 답변기로 바꿔 끼우기 위한 의존성."""
    return rag_answer


def get_agent() -> Agent:
    """신고 대화 에이전트(ai /report/turn). 테스트에서 가짜로 바꿔 끼우기 위한 의존성."""
    return report_turn


# 의존성은 Annotated로 선언 (FastAPI 권장 방식, ruff B008 대응)
DbSession = Annotated[Session, Depends(get_db)]
Classifier = Annotated[IntentClassifier, Depends(get_intent_classifier)]
PhraserDep = Annotated[Phraser, Depends(get_phraser)]
JudgerDep = Annotated[Judger, Depends(get_judger)]
AgentDep = Annotated[Agent, Depends(get_agent)]
AnswererDep = Annotated[Answerer, Depends(get_answerer)]
PhotoStorageDep = Annotated[PhotoStorage, Depends(get_photo_storage)]


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
    photos: PhotoStorageDep,
) -> Response:
    """진행 중이던 신고 흐름을 끝냄 — 프론트가 페이지를 새로 열 때(새로고침) 한 번 호출.

    화면은 새로고침하면 대화 기록 없이 인사말만 보이는데 서버는 같은 session_id의 이전 대화를 기억하고
    있어서, 끝나지 않은 신고에 새 입력이 "정정"으로 이어붙는 문제를 막는다. 세션은 그대로 둠
    (본인 신고 조회가 session_id로 본인 확인을 하므로). 진행 중인 신고가 없으면 아무 일도 안 함.
    올려 둔 대기 사진(1-10)도 지움 — 새로 연 화면엔 사진 칩이 없으므로. 저장소 실패는 무시.
    """
    if db.get(ChatSession, session_id) is None:
        raise HTTPException(status_code=404, detail="대화 세션을 찾을 수 없어요.")
    try:
        photos.delete_pending(session_id)
    except StorageError as e:
        logger.warning("새로고침 때 대기 사진 삭제 실패 session=%s: %s", session_id, e)
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
    judger: JudgerDep,
    agent: AgentDep,
    photos: PhotoStorageDep,
    answerer: AnswererDep,
) -> ChatReply | StreamingResponse:
    if db.get(ChatSession, session_id) is None:
        raise HTTPException(status_code=404, detail="대화 세션을 찾을 수 없어요.")
    # 학번·전화번호는 받자마자 지움 → 저장·분류·판정·접수 설명이 전부 마스킹된 값만 봄 (1-23, 명세 11장)
    text = mask_pii(body.content.strip())
    if not text:
        raise HTTPException(status_code=422, detail="메시지가 비어 있어요.")
    if body.draft is not None:
        d = body.draft
        body.draft = d.model_copy(update={
            "building": mask_optional(d.building), "floor": mask_optional(d.floor),
            "detail": mask_optional(d.detail), "description": mask_optional(d.description),
        })

    history = _recent_history(db, session_id)
    draft = collect_draft(history)
    user_msg = _add_message(db, session_id, ChatRole.USER, text, _next_ts(history))
    action = body.action
    say_history = [HistoryItem(role=m.role.value, content=m.content) for m in history[-3:]]
    say_history.append(HistoryItem(role="user", content=text))

    def phrase(kind: str, base: str, must: list[str]) -> str:
        return phraser(kind, base, must, say_history)

    pending_photo: list[PhotoData | None] = []  # 이 요청에서 대기 사진을 한 번만 가져오려는 캐시 (1-10)

    def judge_photo() -> PhotoData | None:
        if not pending_photo:
            try:
                pending_photo.append(photos.download_pending(session_id))
            except StorageError as e:  # 사진을 못 가져와도 글로 판정 — 접수는 막지 않음
                logger.warning("판정용 대기 사진 가져오기 실패: %s", e)
                pending_photo.append(None)
        return pending_photo[0]

    def judge(slots: ReportSlots, judged_text: str) -> ReportSlots:
        return _judged(db, slots, judged_text, judger, judge_photo)

    def attach(report_id: uuid.UUID) -> str | None:
        # 사진 저장소 문제로 신고 접수가 실패하면 안 됨 → 로그만 남기고 사진 없이 접수 (작업 1-10)
        try:
            return photos.attach_to_report(session_id, report_id)
        except StorageError as e:
            logger.warning("접수 때 사진 붙이기 실패 report=%s: %s", report_id, e)
            return None

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
        return _switch_to_inquiry(db, session_id, user_msg, draft, answerer)

    # 1-0) 에이전트가 진행하던 신고 대화 → 에이전트가 이어서 처리 (실패하면 아래 규칙 기반 흐름으로 대체)
    if draft.in_progress and draft.last_kind == ra.AGENT_KIND:
        user_msg.intent = ChatIntent.REPORT
        if action == "cancel_report" or (action is None and is_cancel(text)):
            return _cancel(db, session_id, user_msg)
        agent_text = "네, 접수해 주세요" if action == "confirm_report" else text
        tail = ra.report_tail(history)
        if tail.prev_action == "confirm" and (action == "confirm_report" or is_confirm(agent_text)) \
                and tail.state is not None:
            return _create_from_agent(
                db, session_id, user_msg, tail.state, tail.user_texts, judge, attach
            )
        try:
            return _agent_reply(db, session_id, user_msg, tail, agent_text, agent, judge, attach)
        except AiServiceError as e:
            logger.warning("신고 에이전트 실패 → 규칙 기반 흐름으로 대체: %s", e)
            user_msg.debug_payload = {"agent_error": str(e)[:300]}

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
                phrase, intro=True, judge=judge, attach=attach,
            )
        if draft.confirming and action is None and is_edit(text) and not is_confirm(text):
            return _ask_edit(db, session_id, user_msg, draft, phrase)
        confirm = draft.in_progress and (
            action == "confirm_report" or (draft.confirming and action is None and is_confirm(text))
        )
        if action == "confirm_report" and not draft.in_progress:
            # 화면이 오래돼 서버엔 진행 중인 신고가 없음 → 폼에 적힌 내용으로 새로 시작해 요약부터
            texts = [t for t in [(body.draft.description if body.draft else None)] if t]
            return _report_step(
                db, session_id, user_msg, texts, texts, set(), False, phrase, judge=judge, attach=attach,
            )
        if confirm:
            texts, descs = list(draft.user_texts), list(draft.desc_texts)
        else:
            texts, descs = draft.with_reply(text)  # 위치 답변·질문은 상황에서 빼는 규칙 적용
        return _report_step(
            db, session_id, user_msg, texts, descs, draft.asked, draft.safety_concern, phrase,
            confirm=confirm, form=body.draft if confirm else None, judge=judge,
            attach=attach,
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

    if result.intent in ("chitchat", "off_topic") and ra.needs_agent_review(text):
        # 잡담·범위 밖으로 봤지만 "신고해주세요"거나 위험한 말("폭파할게요") — 정해진 한 마디로 끝내지 않고 에이전트가 판단
        result = result.model_copy(update={"intent": "report"})

    if result.intent in ("chitchat", "off_topic"):
        # 인사·잡담·범위 밖 — 신고도 문의도 아니므로 흐름을 만들지 않고 짧게 한 마디만 (Gemini 재호출 없음)
        recent = [m.content for m in history if m.role == ChatRole.ASSISTANT]
        reply_text = pick_reply(result.talk, recent)
        user_msg.debug_payload = {"talk": result.talk}
        _add_message(db, session_id, ChatRole.ASSISTANT, reply_text, _after(user_msg))
        db.commit()  # intent 없이 저장 → 진행 중인 흐름이 없다는 표시 그대로
        return StreamingResponse(_sse_once(reply_text), media_type="text/event-stream")

    if result.intent == "unclear":
        user_msg.intent = ChatIntent.UNCLEAR
        user_msg.debug_payload = {"ai_response": result.model_dump()}  # 신뢰도 낮은 케이스 기록
        question = result.clarifying_question or DEFAULT_CLARIFY
        reply = _add_message(db, session_id, ChatRole.ASSISTANT, question, _after(user_msg))
        reply.intent = ChatIntent.UNCLEAR
        db.commit()
        return Unclear(clarifying_question=question)

    if result.intent == "inquiry":
        return _inquiry_reply(db, session_id, user_msg, text, answerer)

    # 3) 신고 — 에이전트가 먼저 걸러내고(말이 안 되거나 확인이 필요하면 확인 질문부터) 필요한 것만 묻는다.
    #    바로 앞이 에이전트의 거절이었는데 학생이 다시 말하면 그 대화를 이어서 본다 (멀쩡한 신고를 막지 않음)
    user_msg.intent = ChatIntent.REPORT
    tail = _first_tail(history, draft)
    try:
        return _agent_reply(
            db, session_id, user_msg, tail, text, agent, judge, attach,
            first=True, safety_concern=draft.safety_concern or result.safety_concern,
        )
    except AiServiceError as e:
        logger.warning("신고 에이전트 실패 → 규칙 기반 흐름으로 대체: %s", e)
        user_msg.debug_payload = {**(user_msg.debug_payload or {}), "agent_error": str(e)[:300]}

    # 3-0) (대체 경로) 신고로 접수하기 어려운 말 — 흐름을 시작하지 않고 한 번 되물음
    asked_before = bool(history) and (
        history[-1].role == ChatRole.ASSISTANT
        and (history[-1].debug_payload or {}).get("kind") == NON_REPORT_KIND
    )
    not_report = None if asked_before else check_not_report(text)
    if not_report is not None:
        reason, reply_text = not_report
        user_msg.intent = None
        user_msg.debug_payload = {"non_report": reason}
        _add_message(db, session_id, ChatRole.ASSISTANT, reply_text, _after(user_msg), NON_REPORT_KIND)
        db.commit()  # intent 없이 저장 → 신고 흐름이 시작되지 않은 상태 그대로
        return StreamingResponse(_sse_once(reply_text), media_type="text/event-stream")

    # (대체 경로) 먼저 접수를 도와드릴지 물음
    return _offer(
        db, session_id, user_msg, [*draft.user_texts, text], [*draft.desc_texts, text],
        draft.safety_concern or result.safety_concern, phrase,
    )


def _first_tail(history: list[ChatMessage], draft: Draft) -> ra.Tail:
    """새 신고의 첫 턴 — 직전에 에이전트가 거절했거나 애매함 되묻기였으면 그 대화를 이어서 본다."""
    conversation: list[HistoryItem] = []
    texts = list(draft.user_texts)  # 애매함 → 신고로 이어진 경우 원래 문장 포함
    if len(history) >= 2 and history[-1].role == ChatRole.ASSISTANT and (
        history[-1].debug_payload or {}
    ).get("kind") == ra.DECLINE_KIND and history[-2].role == ChatRole.USER:
        texts = [history[-2].content]
        conversation = [
            HistoryItem(role="user", content=history[-2].content),
            HistoryItem(role="assistant", content=history[-1].content),
        ]
    elif texts:
        conversation = [HistoryItem(role="user", content=t) for t in texts]
    return ra.Tail(
        conversation=conversation, state=None, prev_action=None, asked=0,
        student_turns=len(texts), user_texts=texts,
    )


def _agent_slots_filled(state: AgentState, buildings: list[BuildingRef], description: str | None) -> SlotsFilled:
    slots = ra.slots_from_state(state, buildings)
    return _slots_filled(slots, description)


def _agent_reply(
    db: Session,
    session_id: uuid.UUID,
    user_msg: ChatMessage,
    tail: ra.Tail,
    text: str,
    agent: Agent,
    judge: Judge,
    attach: AttachPhoto,
    *,
    first: bool = False,
    safety_concern: bool = False,
) -> ChatReply | StreamingResponse:
    """에이전트에게 이번 턴을 맡기고 정책을 적용한 결과를 응답으로 만든다. AiServiceError는 호출한 쪽이 처리."""
    buildings = load_buildings(db)
    decision = ra.decide(tail, text, [b.name for b in buildings], agent)
    state = decision.state
    texts = [*tail.user_texts, text]
    payload: dict[str, object] = {
        "kind": ra.AGENT_KIND, "action": decision.action, "state": state.model_dump(), "clarify": decision.clarify,
    }
    if safety_concern:
        user_msg.intent_scores = {**(user_msg.intent_scores or {}), "safety_concern": True}
    if decision.action == "submit":
        return _create_from_agent(db, session_id, user_msg, state, texts, judge, attach)
    if decision.action == "cancel":
        return _cancel(db, session_id, user_msg, decision.message)
    if decision.action == "decline":
        user_msg.intent = None if first else ChatIntent.REPORT
        reply = _add_message(
            db, session_id, ChatRole.ASSISTANT, decision.message, _after(user_msg), ra.DECLINE_KIND
        )
        reply.intent = None  # intent 없이 저장 → 신고 흐름 끝 (첫 턴이면 시작되지 않은 상태 그대로)
        db.commit()
        if first:
            return StreamingResponse(_sse_once(decision.message), media_type="text/event-stream")
        return ReportFollowUp(
            follow_up_question=decision.message,
            slots_filled=_agent_slots_filled(state, buildings, None),
        )
    reply = _add_message(db, session_id, ChatRole.ASSISTANT, decision.message, _after(user_msg), payload=payload)
    reply.intent = ChatIntent.REPORT
    db.commit()
    filled = _agent_slots_filled(state, buildings, state.problem or None)
    if decision.action == "confirm":
        return ReportConfirm(
            summary=decision.message, follow_up_question=decision.message,
            slots_filled=filled, choices=decision.choices,
        )
    return ReportFollowUp(
        follow_up_question=decision.message, slots_filled=filled, choices=decision.choices or None
    )


def _cancel(
    db: Session, session_id: uuid.UUID, user_msg: ChatMessage, message: str = CANCELLED
) -> ReportCancelled:
    _add_message(db, session_id, ChatRole.ASSISTANT, message, _after(user_msg))
    db.commit()  # intent 없이 저장 → 신고 흐름 끝 표시
    return ReportCancelled(message=message, follow_up_question=message, slots_filled=SlotsFilled())


def _create_from_agent(
    db: Session,
    session_id: uuid.UUID,
    user_msg: ChatMessage,
    state: AgentState,
    user_texts: list[str],
    judge: Judge,
    attach: AttachPhoto,
) -> ReportCreated:
    slots = ra.slots_from_state(state, load_buildings(db))
    judged_text = "\n".join([state.problem, *user_texts]) if state.problem else "\n".join(user_texts)
    slots = judge(slots, judged_text)
    extra = ""
    pending = [p for p in state.pending_issues if p][:3]
    if pending:
        names = ", ".join(f"'{p}'" for p in pending)
        extra = f" 말씀하신 {names} 건도 따로 접수하시려면 아래 버튼을 눌러 주세요."
    return _create(
        db, session_id, user_msg, slots, ra.description_for(user_texts, state), attach, extra=extra,
        choices=pending or None,
    )


def _just_reported(history: list[ChatMessage]) -> bool:
    """직전 챗봇 메시지가 접수 완료 안내였나."""
    return bool(history) and (
        history[-1].role == ChatRole.ASSISTANT
        and history[-1].intent is None
        and history[-1].content.startswith(DONE_PREFIX)
    )


def _inquiry_reply(
    db: Session, session_id: uuid.UUID, user_msg: ChatMessage, question: str, answerer: Answerer
) -> StreamingResponse:
    """행정 문의 — 학칙·안내·공지를 근거로 답하고 근거(sources)를 함께 저장·전달 (1-4c, 명세 5-1).

    intent=행정문의로 저장하면 collect_draft가 신고 흐름 끝으로 봄. 답변을 못 만들면 의도는 저장하지 않고
    (신고 흐름을 그대로 둠) 503 — 학생이 같은 버튼·질문을 다시 보낼 수 있다.
    """
    try:
        result = answerer(question)
    except AiServiceError as e:
        user_msg.debug_payload = {**(user_msg.debug_payload or {}), "error": "rag_unavailable",
                                  "detail": str(e)[:500]}
        db.commit()
        raise HTTPException(status_code=503, detail="잠시 후 다시 시도해주세요") from None
    sources = [s.model_dump() for s in result.sources]
    user_msg.intent = ChatIntent.INQUIRY
    reply = _add_message(db, session_id, ChatRole.ASSISTANT, result.answer, _after(user_msg))
    reply.intent = ChatIntent.INQUIRY
    reply.sources = sources
    db.commit()
    return StreamingResponse(_sse_once(result.answer, sources), media_type="text/event-stream")


def _switch_to_inquiry(
    db: Session, session_id: uuid.UUID, user_msg: ChatMessage, draft: Draft, answerer: Answerer
) -> StreamingResponse:
    """[안내만 받을래요] — 신고 흐름을 끝내고 첫 발화(`draft.user_texts[0]`, 없으면 이번 메시지)를 행정문의로 답변."""
    question = draft.user_texts[0] if draft.user_texts else user_msg.content
    return _inquiry_reply(db, session_id, user_msg, question, answerer)


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
    judge: Judge,
    attach: AttachPhoto,
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
        slots = judge(slots, judged)  # 최종 위치 기준으로 AI가 카테고리·영향도·긴급도·이유 판정
        return _create(db, session_id, user_msg, slots, form_desc or described or joined, attach)

    slots = judge(slots, judged)  # AI 판정 (실패하면 규칙 기반 값 그대로) — 상황을 말했는지도 여기서 판단
    # 학생이 마지막 답에서 "모르겠어요"라고 했으면 그 항목은 다시 묻지 않음
    unsure = bool(texts) and any(w in texts[-1] for w in UNKNOWN_WORDS)
    affirmed = any("맞" in t for t in texts)  # "4층이 맞아요" 같은 확인
    question = next_question(
        slots, asked, unsure, affirmed, find_ambiguity(texts[0]) if texts else None
    )
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
    base = build_summary(slots, descs, affirmed)
    must = [t for t in [slots.location_text] if t and t in base] + ["접수"]
    if slots.floor_check and not affirmed:
        must.append(cp.floor_label(slots.floor or "4"))
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


def _judged(
    db: Session,
    slots: ReportSlots,
    text: str,
    judger: Judger,
    photo: Callable[[], PhotoData | None] | None = None,
) -> ReportSlots:
    """규칙으로 뽑은 슬롯에 AI 판정을 덮어씀 (1-3b). AI가 실패하면 규칙 기반 값을 그대로 씀 (명세 11장).
    대기 사진이 있으면 함께 보내 AI가 사진도 보고 판정함 (1-10)."""
    try:
        result = judger(text, slots.location_text, load_category_names(db), photo() if photo else None)
    except AiServiceError as e:
        logger.warning("AI 판정 실패 → 규칙 기반 판정 사용: %s", e)
        return slots
    return apply_judgement(slots, result, text)


def _create(
    db: Session,
    session_id: uuid.UUID,
    user_msg: ChatMessage,
    slots: ReportSlots,
    description: str,
    attach: AttachPhoto,
    extra: str = "",
    choices: list[str] | None = None,
) -> ReportCreated:
    report, category = create_report(db, session_id, slots, description=description)
    photo_path = attach(report.id)  # 대기 사진이 있으면 이 신고로 옮김 (1-10)
    if photo_path:
        report.photo_url = photo_path  # URL이 아니라 저장소 경로 — 관리자 조회 때 임시 링크로 바꿈
    where = f"{slots.location_text}에서 생긴 " if slots.location_text else ""
    done = (
        f"{DONE_PREFIX}! 접수번호는 {report.display_no}번이에요. "
        f"{where}{category.name} 문제로 분류했어요. "
        f"{judge_reason(slots, report.priority.value)} 담당 부서에서 확인 후 처리할게요.{extra}"
        f"{PHOTO_SENT if photo_path else ''}"
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
        choices=choices,
    )


def _add_message(
    db: Session,
    session_id: uuid.UUID,
    role: ChatRole,
    content: str,
    created_at: datetime,
    kind: str | None = None,  # 챗봇 메시지 종류 — 말투가 바뀌어도 대화 재구성에서 알아보게 저장
    payload: dict[str, object] | None = None,  # kind 외에 저장할 값 (에이전트 상태 등)
) -> ChatMessage:
    msg = ChatMessage(session_id=session_id, role=role, content=content, created_at=created_at)
    if payload:
        msg.debug_payload = payload
    elif kind:
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


def _sse_once(text: str, sources: list[dict[str, object]] | None = None) -> Iterator[str]:
    """답변 전체를 delta 한 번 + done 이벤트(근거 sources 포함)로 보냄 (ai 쪽 실시간 스트리밍은 3순위 1-22)."""
    yield f"data: {json.dumps({'delta': text}, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'done': True, 'sources': sources or []}, ensure_ascii=False)}\n\n"
