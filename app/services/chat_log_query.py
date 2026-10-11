"""관리자 행정 문의 로그 조회 — 작업 1-18 (명세서 5-1 "문의 로그 API 형식").

- 대상: 학생 질문(role=user) 중 intent가 지정한 값(기본 문의)인 메시지. 최신순.
- 답변: 같은 세션에서 그 질문 바로 다음에 저장된 챗봇 메시지(role=assistant) 1개. 질문 다음 메시지가
  학생 메시지면(답변 실패) 답변 없음.
- 신뢰도 낮음(low_confidence): ① debug_payload가 있음(되묻기·ai/RAG 오류 기록 — chat.py 참고) 또는
  ② 의도 점수 중 큰 쪽이 LOW_CONFIDENCE_SCORE 미만. 점수가 없는 질문([안내만 받을래요]로 넘어온 문의)은 ②에 안 걸림.
- 질문 페이지를 먼저 고르고, 그 세션들의 메시지만 한 번 더 읽어 답변을 짝지음(쿼리 2번 + count 1번).
"""
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Integer, Select, func, or_, select
from sqlalchemy.orm import Session

from app.models.chat import ChatMessage
from app.models.enums import ChatIntent, ChatRole
from app.schemas.chat_logs import ChatLogItem, ChatLogList

# 의도 점수(0~100) 중 큰 쪽이 이보다 낮으면 신뢰도 낮음 — 두 점수가 70:30보다 가까우면 애매한 질문으로 본다
LOW_CONFIDENCE_SCORE = 70


def is_low_confidence(intent_scores: dict[str, Any] | None, debug_payload: dict[str, Any] | None) -> bool:
    if debug_payload:
        return True
    if not intent_scores:
        return False
    scores = [v for k, v in intent_scores.items() if k.endswith("_score") and isinstance(v, int | float)]
    return bool(scores) and max(scores) < LOW_CONFIDENCE_SCORE


def _low_confidence_sql() -> Any:
    """is_low_confidence와 같은 기준의 SQL 조건 (필터용)."""
    scores = ChatMessage.intent_scores
    top = func.greatest(
        scores["report_score"].astext.cast(Integer),
        scores["inquiry_score"].astext.cast(Integer),
    )
    return or_(ChatMessage.debug_payload.is_not(None), func.coalesce(top, 100) < LOW_CONFIDENCE_SCORE)


def _questions(intent: ChatIntent, days: int | None, low_confidence: bool) -> Select[ChatMessage]:
    stmt = select(ChatMessage).where(ChatMessage.role == ChatRole.USER, ChatMessage.intent == intent)
    if days is not None:
        stmt = stmt.where(ChatMessage.created_at >= datetime.now(UTC) - timedelta(days=days))
    if low_confidence:
        stmt = stmt.where(_low_confidence_sql())
    return stmt


def _answers(db: Session, questions: list[ChatMessage]) -> dict[uuid.UUID, ChatMessage]:
    """질문 id → 바로 다음 메시지가 챗봇 답변이면 그 메시지."""
    if not questions:
        return {}
    session_ids = {q.session_id for q in questions}
    since = min(q.created_at for q in questions)
    later = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id.in_(session_ids), ChatMessage.created_at > since)
        .order_by(ChatMessage.session_id, ChatMessage.created_at)
    ).all()
    by_session: dict[uuid.UUID, list[ChatMessage]] = {}
    for m in later:
        by_session.setdefault(m.session_id, []).append(m)
    out: dict[uuid.UUID, ChatMessage] = {}
    for q in questions:
        nxt = next((m for m in by_session.get(q.session_id, []) if m.created_at > q.created_at), None)
        if nxt is not None and nxt.role == ChatRole.ASSISTANT:
            out[q.id] = nxt
    return out


def list_chat_logs(
    db: Session,
    *,
    intent: ChatIntent = ChatIntent.INQUIRY,
    days: int | None = None,
    low_confidence: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> ChatLogList:
    base = _questions(intent, days, low_confidence)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    questions = list(
        db.scalars(base.order_by(ChatMessage.created_at.desc(), ChatMessage.id).limit(limit).offset(offset)).all()
    )
    answers = _answers(db, questions)
    items = []
    for q in questions:
        a = answers.get(q.id)
        items.append(ChatLogItem(
            id=q.id,
            asked_at=q.created_at,
            question=q.content,
            answer=a.content if a else None,
            intent=q.intent.value if q.intent else None,
            intent_scores=q.intent_scores,
            low_confidence=is_low_confidence(q.intent_scores, q.debug_payload),
            sources=a.sources if a else None,
        ))
    return ChatLogList(total=total, items=items)
