"""로그인 계정 기준 이전 대화 — claim·목록·상세 (작업 1-27, 명세서 5-1).

- DB 없이: 제목 규칙
- DB 통합: TEST_DATABASE_URL(마이그레이션 끝난 테스트용 빈 DB)이 있을 때만 — 운영 DB 금지!
  매번 새 아이디·새 세션을 만들어 같은 DB로 다시 돌려도 깨지지 않게 함.
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

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.models import Admin, ChatMessage
from app.models.enums import ChatIntent, ChatRole
from app.services import chat_history, login_guard
from app.services.chat_history import make_title

needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL 없음 — DB 통합테스트 생략")
PASSWORD = "test-only-password"
HIDDEN = {"intent", "intent_scores", "debug_payload", "draft", "user_identifier", "user_id"}


# ── DB 없이 ───────────────────────────────────────────────────────────────────
def test_title_rules() -> None:
    assert make_title("3층 정수기가 고장났어요") == "3층 정수기가 고장났어요"
    long = "가" * 40
    assert make_title(long) == "가" * 30 + "…"
    assert make_title("(사진을 보냈어요)") == "사진 대화"
    assert make_title("  (사진 첨부)  ") == "사진 대화"
    assert make_title(None) == "사진 대화"  # 학생 글이 없는 세션(챗봇 메시지만)
    assert make_title("정수기 고장\n\n  2층") == "정수기 고장 2층"


# ── DB 통합 ───────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    with TestClient(app) as c:
        yield c


def _signup(client: TestClient) -> dict[str, str]:
    login_guard.reset_all()
    login_id = f"t_{uuid.uuid4().hex[:10]}"
    res = client.post("/api/v1/auth/signup", json={"login_id": login_id, "password": PASSWORD, "name": "김학생"})
    assert res.status_code == 201
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _session(client: TestClient) -> str:
    res = client.post("/api/v1/chat/sessions")
    assert res.status_code == 201
    return str(res.json()["session_id"])


def _add(session_id: str, texts: list[tuple[ChatRole, str]], start: datetime | None = None,
         sources: list[dict[str, str]] | None = None) -> None:
    """메시지를 DB에 직접 넣음 — 챗봇(ai) 없이 대화 기록만 만들기 위해. 숨겨야 하는 칼럼도 일부러 채움."""
    t = start or datetime.now(UTC)
    db = get_sessionmaker()()
    for i, (role, content) in enumerate(texts):
        db.add(ChatMessage(
            session_id=uuid.UUID(session_id), role=role, content=content, created_at=t + timedelta(microseconds=i),
            intent=ChatIntent.INQUIRY, intent_scores={"report_score": 1, "inquiry_score": 99},
            debug_payload={"kind": "test", "draft": {"x": 1}},
            sources=sources if role == ChatRole.ASSISTANT else None,
        ))
    db.commit()
    db.close()


@needs_db
def test_claim_list_and_detail(client: TestClient) -> None:
    me = _signup(client)
    old, new, empty, anon = _session(client), _session(client), _session(client), _session(client)
    now = datetime.now(UTC)
    _add(old, [(ChatRole.USER, "휴학 신청은 어떻게 하나요?"), (ChatRole.ASSISTANT, "학사 공지에 따르면…")],
         start=now - timedelta(hours=1), sources=[{"title": "학칙", "article_no": "제10조", "url": "https://x"}])
    _add(new, [(ChatRole.USER, "(사진을 보냈어요)"), (ChatRole.ASSISTANT, "어디인가요?"),
               (ChatRole.USER, "본관 2층")], start=now)
    _add(anon, [(ChatRole.USER, "로그인 안 한 대화")])  # claim하지 않음 → 목록에 안 나옴
    for sid in (old, new, empty):
        assert client.post(f"/api/v1/chat/sessions/{sid}/claim", headers=me).status_code == 204
    # 두 번 불러도 204
    assert client.post(f"/api/v1/chat/sessions/{old}/claim", headers=me).status_code == 204

    res = client.get("/api/v1/chat/my-sessions", headers=me)
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 2  # 메시지 없는 세션(empty) 제외, 비로그인 세션(anon) 제외
    assert [i["session_id"] for i in body["items"]] == [new, old]  # 최근 대화순
    assert body["items"][0]["title"] == "사진 대화" and body["items"][0]["message_count"] == 3
    assert body["items"][1]["title"] == "휴학 신청은 어떻게 하나요?"
    assert set(body["items"][0]) == {"session_id", "title", "started_at", "last_message_at", "message_count"}
    # limit·offset
    page = client.get("/api/v1/chat/my-sessions?limit=1&offset=1", headers=me).json()
    assert page["total"] == 2 and [i["session_id"] for i in page["items"]] == [old]
    assert client.get("/api/v1/chat/my-sessions?limit=101", headers=me).status_code == 422

    res = client.get(f"/api/v1/chat/my-sessions/{old}", headers=me)
    assert res.status_code == 200
    detail = res.json()
    assert detail["session_id"] == old and detail["truncated"] is False
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert "sources" not in detail["messages"][0]  # 학생 메시지엔 필드 생략
    assert detail["messages"][1]["sources"][0]["article_no"] == "제10조"
    for m in detail["messages"]:
        assert not HIDDEN & set(m)
    assert not HIDDEN & set(detail)
    assert "user_identifier" not in res.text and "intent_scores" not in res.text and "draft" not in res.text


@needs_db
def test_other_account_and_tokens(client: TestClient) -> None:
    a, b = _signup(client), _signup(client)
    sid = _session(client)
    _add(sid, [(ChatRole.USER, "a의 대화")])
    assert client.post(f"/api/v1/chat/sessions/{sid}/claim", headers=a).status_code == 204
    assert client.post(f"/api/v1/chat/sessions/{sid}/claim", headers=b).status_code == 409  # 남의 세션
    assert client.get(f"/api/v1/chat/my-sessions/{sid}", headers=b).status_code == 404  # 남의 세션 = 404
    assert client.get(f"/api/v1/chat/my-sessions/{uuid.uuid4()}", headers=a).status_code == 404  # 없는 세션
    assert client.post(f"/api/v1/chat/sessions/{uuid.uuid4()}/claim", headers=a).status_code == 404
    assert client.get("/api/v1/chat/my-sessions", headers=b).json()["total"] == 0
    # 토큰 없음·위조 → 401
    assert client.get("/api/v1/chat/my-sessions").status_code == 401
    assert client.post(f"/api/v1/chat/sessions/{sid}/claim").status_code == 401
    assert client.get("/api/v1/chat/my-sessions", headers={"Authorization": "Bearer nope"}).status_code == 401
    # 관리자 토큰 → 401
    admin_id = f"t_{uuid.uuid4().hex[:10]}"
    db = get_sessionmaker()()
    db.add(Admin(login_id=admin_id, password_hash=bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode(),
                 name="테스트관리자"))
    db.commit()
    db.close()
    login_guard.reset_all()
    res = client.post("/api/v1/admin/auth/login", json={"login_id": admin_id, "password": PASSWORD})
    admin = {"Authorization": f"Bearer {res.json()['access_token']}"}
    assert client.get("/api/v1/chat/my-sessions", headers=admin).status_code == 401
    assert client.post(f"/api/v1/chat/sessions/{sid}/claim", headers=admin).status_code == 401


@needs_db
def test_detail_truncates_to_latest(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_history, "MAX_MESSAGES", 3)
    me = _signup(client)
    sid = _session(client)
    _add(sid, [(ChatRole.USER, f"메시지 {i}") for i in range(5)])
    assert client.post(f"/api/v1/chat/sessions/{sid}/claim", headers=me).status_code == 204
    detail = client.get(f"/api/v1/chat/my-sessions/{sid}", headers=me).json()
    assert detail["truncated"] is True
    assert [m["content"] for m in detail["messages"]] == ["메시지 2", "메시지 3", "메시지 4"]  # 가장 최근 3개, 오래된 순
    # 목록의 개수·제목은 자르지 않음
    item = client.get("/api/v1/chat/my-sessions", headers=me).json()["items"][0]
    assert item["message_count"] == 5 and item["title"] == "메시지 0"
