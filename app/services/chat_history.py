"""로그인 계정 기준 이전 대화 조회 (작업 1-27, 명세서 5-1).

- claim: 로그인한 학생이 지금 대화 세션을 내 계정에 연결(chat_sessions.user_id). 연결 전에 오간 대화도 함께 묶임.
- 목록·상세: 내 계정에 연결된 세션만, 읽기 전용. 남의 세션과 없는 세션은 구분하지 않고 None(→ 404).
- 내용은 저장 전에 이미 학번·전화번호가 마스킹돼 있음(1-23) — 여기서 다시 마스킹하지 않는다.
"""
import enum
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.models.chat import ChatMessage, ChatSession
from app.models.enums import ChatRole
from app.schemas.chat_history import HistoryMessage, MySessionDetail, MySessionItem, MySessionList

TITLE_LEN = 30
PHOTO_TITLE = "사진 대화"
MAX_MESSAGES = 200  # 상세에서 한 번에 보내는 최대 메시지 수 — 넘으면 가장 최근 것만
# 사진만 보낸 턴에 content로 저장되는 자리표시 (chat.py·report_agent.py의 PHOTO_ONLY_TEXT와 같은 글 —
# 1-26이 그 파일들을 고치는 중이라 import하지 않고 글로 비교)
PHOTO_PLACEHOLDERS = ("(사진을 보냈어요)", "(사진 첨부)")


class ClaimResult(enum.Enum):
    OK = "ok"  # 새로 연결했거나 이미 내 세션
    NOT_FOUND = "not_found"
    TAKEN = "taken"  # 다른 계정이 연결한 세션


def claim_session(db: Session, session_id: uuid.UUID, user_id: uuid.UUID) -> ClaimResult:
    # 아직 아무 계정에도 연결되지 않은 세션만 바꾼다 — 동시에 두 계정이 claim해도 한쪽만 성공
    res = db.execute(
        update(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.user_id.is_(None))
        .values(user_id=user_id)
    )
    db.commit()
    if getattr(res, "rowcount", 0):
        return ClaimResult.OK
    owner = db.execute(select(ChatSession.user_id).where(ChatSession.id == session_id)).one_or_none()
    if owner is None:
        return ClaimResult.NOT_FOUND
    return ClaimResult.OK if owner[0] == user_id else ClaimResult.TAKEN


def make_title(first_user_text: str | None) -> str:
    """첫 학생 메시지 앞 30자. 사진 자리표시만 있거나 학생 글이 없으면 "사진 대화"."""
    text = first_user_text or ""
    for mark in PHOTO_PLACEHOLDERS:
        text = text.replace(mark, "")
    text = " ".join(text.split())
    if not text:
        return PHOTO_TITLE
    return text if len(text) <= TITLE_LEN else text[:TITLE_LEN] + "…"


def list_my_sessions(db: Session, user_id: uuid.UUID, limit: int, offset: int) -> MySessionList:
    """최근 대화순(마지막 메시지 시각), 메시지가 없는 세션 제외."""
    stats = (
        select(
            ChatMessage.session_id.label("sid"),
            func.max(ChatMessage.created_at).label("last_at"),
            func.count(ChatMessage.id).label("cnt"),
        )
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .where(ChatSession.user_id == user_id)
        .group_by(ChatMessage.session_id)
        .subquery()
    )
    total = db.scalar(select(func.count()).select_from(stats)) or 0
    rows = db.execute(
        select(ChatSession.id, ChatSession.started_at, stats.c.last_at, stats.c.cnt)
        .join(stats, stats.c.sid == ChatSession.id)
        .order_by(stats.c.last_at.desc(), ChatSession.id)
        .limit(limit)
        .offset(offset)
    ).all()
    ids = [r[0] for r in rows]
    firsts: dict[uuid.UUID, str] = {}
    if ids:
        # 세션마다 첫 학생 메시지 (DISTINCT ON — PostgreSQL)
        first_rows = db.execute(
            select(ChatMessage.session_id, ChatMessage.content)
            .where(ChatMessage.session_id.in_(ids), ChatMessage.role == ChatRole.USER)
            .order_by(ChatMessage.session_id, ChatMessage.created_at)
            .ext(distinct_on(ChatMessage.session_id))
        ).all()
        firsts = {sid: content for sid, content in first_rows}
    items = [
        MySessionItem(
            session_id=sid,
            title=make_title(firsts.get(sid)),
            started_at=started,
            last_message_at=last_at,
            message_count=cnt,
        )
        for sid, started, last_at, cnt in rows
    ]
    return MySessionList(total=total, items=items)


def get_my_session(db: Session, user_id: uuid.UUID, session_id: uuid.UUID) -> MySessionDetail | None:
    """None = 내 계정 세션이 아님(없거나 남의 것) → 404."""
    session = db.get(ChatSession, session_id)
    if session is None or session.user_id != user_id:
        return None
    recent = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(MAX_MESSAGES + 1)
    ).all()
    truncated = len(recent) > MAX_MESSAGES
    msgs = list(reversed(recent[:MAX_MESSAGES]))
    return MySessionDetail(
        session_id=session.id,
        started_at=session.started_at,
        messages=[
            HistoryMessage(role=m.role, content=m.content, created_at=m.created_at, sources=m.sources or None)
            for m in msgs
        ],
        truncated=truncated,
    )
