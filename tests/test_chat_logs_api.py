"""관리자 행정 문의 로그 API 테스트 (작업 1-18, 명세서 5-1 "문의 로그 API 형식").

- 신뢰도 낮음 기준 단위테스트: DB 없이 항상 실행
- /admin/chat-logs 통합테스트: TEST_DATABASE_URL(마이그레이션 끝난 테스트용 빈 DB)이 있을 때만 — 운영 DB 금지!
  다른 테스트가 같은 DB에 메시지를 넣으므로 이 테스트가 만든 세션의 질문만 골라 비교하고, 끝나면 세션째 지운다.
"""
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB
    os.environ["JWT_SECRET"] = "integration-test-secret-at-least-32-bytes!!"

import bcrypt
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.models import Admin
from app.models.chat import ChatMessage, ChatSession
from app.models.enums import ChatIntent, ChatRole
from app.services.chat_log_query import is_low_confidence

# ── 단위테스트: 신뢰도 낮음 기준 ──

def test_low_confidence_rules() -> None:
    assert is_low_confidence({"report_score": 5, "inquiry_score": 95}, None) is False
    assert is_low_confidence({"report_score": 40, "inquiry_score": 60}, None) is True    # 둘 다 70 미만
    assert is_low_confidence({"report_score": 5, "inquiry_score": 95}, {"error": "rag_unavailable"}) is True
    assert is_low_confidence(None, None) is False                                        # 점수 없음([안내만 받을래요])
    assert is_low_confidence({"safety_concern": True}, None) is False                    # 점수 칸이 없으면 안 걸림


# ── 통합테스트: /admin/chat-logs ──

needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL 없음 — DB 통합테스트 생략")

PASSWORD = "test-only-password"
BASE = "/api/v1/admin"


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def headers(client: TestClient) -> dict[str, str]:
    db = get_sessionmaker()()
    a = Admin(
        login_id=f"t_{uuid.uuid4().hex[:8]}",
        password_hash=bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode(),
        name="테스트관리자",
    )
    db.add(a)
    db.commit()
    login_id = a.login_id
    db.close()
    res = client.post(f"{BASE}/auth/login", json={"login_id": login_id, "password": PASSWORD})
    assert res.status_code == 200
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@pytest.fixture()
def logs() -> Iterator[dict[str, str]]:
    """세션 하나에 문의 4개(+ 신고 1개). 질문 내용 → 메시지 id. 끝나면 세션째 지움(메시지는 CASCADE)."""
    now = datetime.now(UTC)
    db = get_sessionmaker()()
    s = ChatSession(user_identifier=f"log-test-{uuid.uuid4().hex[:8]}")
    db.add(s)
    db.flush()
    ids: dict[str, str] = {}

    def msg(role: ChatRole, content: str, minutes_ago: float, **kw: object) -> ChatMessage:
        m = ChatMessage(session_id=s.id, role=role, content=content,
                        created_at=now - timedelta(minutes=minutes_ago), **kw)
        db.add(m)
        db.flush()
        if role == ChatRole.USER:
            ids[content] = str(m.id)
        return m

    src = [{"title": "학칙", "article_no": "제29조", "url": None}]
    sure = {"report_score": 5, "inquiry_score": 95, "safety_concern": False}
    msg(ChatRole.USER, "휴학 어떻게 해요?", 50, intent=ChatIntent.INQUIRY, intent_scores=sure)
    msg(ChatRole.ASSISTANT, "휴학은 ...", 49.9, intent=ChatIntent.INQUIRY, sources=src)
    msg(ChatRole.USER, "북악관 화장실 고장", 40, intent=ChatIntent.REPORT, intent_scores=sure)
    msg(ChatRole.USER, "장학금 애매한 질문", 30, intent=ChatIntent.INQUIRY,
        intent_scores={"report_score": 45, "inquiry_score": 55})
    msg(ChatRole.ASSISTANT, "장학금은 ...", 29.9, intent=ChatIntent.INQUIRY, sources=[])
    msg(ChatRole.USER, "답변 실패한 질문", 20, intent=ChatIntent.INQUIRY, intent_scores=sure,
        debug_payload={"error": "rag_unavailable"})
    msg(ChatRole.USER, "다시 물어봄", 19, intent=ChatIntent.INQUIRY, intent_scores=sure)
    msg(ChatRole.ASSISTANT, "다시 답함", 18.9, intent=ChatIntent.INQUIRY, sources=src)
    msg(ChatRole.USER, "40일 전 질문", 60 * 24 * 40, intent=ChatIntent.INQUIRY, intent_scores=sure)
    db.commit()
    sid = s.id
    db.close()
    yield ids

    db = get_sessionmaker()()
    db.execute(delete(ChatSession).where(ChatSession.id == sid))
    db.commit()
    db.close()


def _mine(body: dict[str, object], ids: dict[str, str]) -> list[dict[str, object]]:
    mine = set(ids.values())
    items = body["items"]
    assert isinstance(items, list)
    return [i for i in items if i["id"] in mine]


@needs_db
def test_requires_token(client: TestClient) -> None:
    assert client.get(f"{BASE}/chat-logs").status_code == 401


@needs_db
def test_pairs_question_and_answer(client: TestClient, headers: dict[str, str], logs: dict[str, str]) -> None:
    res = client.get(f"{BASE}/chat-logs", params={"limit": 200}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"total", "items"}
    rows = _mine(body, logs)
    assert [r["question"] for r in rows] == ["다시 물어봄", "답변 실패한 질문", "장학금 애매한 질문", "휴학 어떻게 해요?",
                                             "40일 전 질문"]  # 최신순, 신고는 빠짐
    by_q = {r["question"]: r for r in rows}
    assert set(by_q["휴학 어떻게 해요?"]) == {"id", "asked_at", "question", "answer", "intent", "intent_scores",
                                         "low_confidence", "sources"}
    assert by_q["휴학 어떻게 해요?"]["answer"] == "휴학은 ..."
    assert by_q["휴학 어떻게 해요?"]["sources"] == [{"title": "학칙", "article_no": "제29조", "url": None}]
    assert by_q["휴학 어떻게 해요?"]["intent"] == "문의" and by_q["휴학 어떻게 해요?"]["low_confidence"] is False
    # 바로 다음이 학생 메시지 → 답변 없음 (다음 질문의 답을 끌어오지 않음)
    assert by_q["답변 실패한 질문"]["answer"] is None and by_q["답변 실패한 질문"]["sources"] is None
    assert by_q["답변 실패한 질문"]["low_confidence"] is True
    assert by_q["장학금 애매한 질문"]["low_confidence"] is True and by_q["장학금 애매한 질문"]["sources"] == []


@needs_db
def test_filters(client: TestClient, headers: dict[str, str], logs: dict[str, str]) -> None:
    def questions(**params: object) -> list[object]:
        res = client.get(f"{BASE}/chat-logs", params={"limit": 200, **params}, headers=headers)
        assert res.status_code == 200, res.text
        return [r["question"] for r in _mine(res.json(), logs)]

    assert "40일 전 질문" not in questions(days=30)
    assert questions(low_confidence="true") == ["답변 실패한 질문", "장학금 애매한 질문"]
    # 신고 질문은 다른 테스트도 많이 만들어서(이 세션 것이 200건 밖일 수 있음) 의도만 확인
    reports = client.get(f"{BASE}/chat-logs", params={"intent": "신고"}, headers=headers).json()["items"]
    assert reports and {r["intent"] for r in reports} == {"신고"}


@needs_db
def test_paging_and_validation(client: TestClient, headers: dict[str, str], logs: dict[str, str]) -> None:
    full = client.get(f"{BASE}/chat-logs", params={"limit": 200}, headers=headers).json()
    page = client.get(f"{BASE}/chat-logs", params={"limit": 2, "offset": 1}, headers=headers).json()
    assert page["total"] == full["total"] and len(page["items"]) == 2
    assert [i["id"] for i in page["items"]] == [i["id"] for i in full["items"][1:3]]
    for bad in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"days": 0}, {"intent": "잡담"}):
        assert client.get(f"{BASE}/chat-logs", params=bad, headers=headers).status_code == 422
