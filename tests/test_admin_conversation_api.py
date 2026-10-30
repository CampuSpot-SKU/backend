"""신고 대화 열람 API 통합테스트 (작업 1-25, 명세서 5-1 `/admin/reports/{id}/conversation`).

TEST_DATABASE_URL(마이그레이션 끝난 테스트용 빈 DB)이 있을 때만 — 운영 DB 금지!
이 테스트 전용 카테고리·세션·신고를 만들고 끝나면 지운다.
"""
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL 없음 — DB 통합테스트 생략")
if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB
    os.environ["JWT_SECRET"] = "integration-test-secret-at-least-32-bytes!!"

import bcrypt
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.models import Admin, Category, Report
from app.models.chat import ChatMessage, ChatSession
from app.models.enums import ChatIntent, ChatRole, Priority, ReportStatus
from app.routers.chat import get_intent_classifier, get_judger
from app.services.ai_client import AiServiceError, HistoryItem, IntentResult
from app.services.photo_storage import get_photo_storage
from tests.fake_photo_storage import FAKE_STORAGE

PASSWORD = "test-only-password"
BASE = "/api/v1/admin/reports"


def fake_classifier(text: str, history: list[HistoryItem]) -> IntentResult:
    return IntentResult(intent="report", report_score=95, inquiry_score=5)


def fake_judger(*_args: object, **_kwargs: object) -> None:
    raise AiServiceError("ai judge down")  # AI 판정 실패 → 규칙 기반 판정 (기존 챗 테스트와 같은 방식)


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    # 종단 간 테스트(실제 챗봇 흐름)에서 ai·Gemini 대신 쓰는 가짜 — 관리자 API에는 영향 없음
    app.dependency_overrides[get_intent_classifier] = lambda: fake_classifier
    app.dependency_overrides[get_judger] = lambda: fake_judger
    app.dependency_overrides[get_photo_storage] = lambda: FAKE_STORAGE
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def headers(client: TestClient) -> dict[str, str]:
    db = get_sessionmaker()()
    a = Admin(login_id=f"t_{uuid.uuid4().hex[:8]}",
              password_hash=bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode(), name="테스트관리자")
    db.add(a)
    db.commit()
    login_id = a.login_id
    db.close()
    res = client.post("/api/v1/admin/auth/login", json={"login_id": login_id, "password": PASSWORD})
    assert res.status_code == 200
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@pytest.fixture()
def cases() -> Iterator[dict[str, uuid.UUID]]:
    """report: 문의 → 신고 흐름(요약 확인 포함) → 접수 완료 → 문의. no_session: 세션 없는 신고(데모 데이터처럼)."""
    t0 = datetime.now(UTC) - timedelta(hours=1)
    db = get_sessionmaker()()
    cat = Category(name=f"대화테스트-{uuid.uuid4().hex[:6]}", is_active=True)
    sess = ChatSession(user_identifier=f"conv-test-{uuid.uuid4().hex[:8]}")
    db.add_all([cat, sess])
    db.flush()

    def msg(sec: int, role: ChatRole, content: str, intent: ChatIntent | None) -> None:
        db.add(ChatMessage(session_id=sess.id, role=role, content=content, intent=intent,
                           created_at=t0 + timedelta(seconds=sec), debug_payload={"kind": "내부", "secret": "x"}))

    msg(0, ChatRole.USER, "휴학 언제예요?", ChatIntent.INQUIRY)
    msg(2, ChatRole.ASSISTANT, "공지에 따르면…", ChatIntent.INQUIRY)
    msg(20, ChatRole.USER, "은주1관 화장실 휴지가 없어요", ChatIntent.REPORT)
    msg(21, ChatRole.ASSISTANT, "몇 층인가요?", ChatIntent.REPORT)
    msg(30, ChatRole.USER, "3층이요", ChatIntent.REPORT)
    msg(31, ChatRole.ASSISTANT, "이대로 접수할까요?", ChatIntent.REPORT)
    msg(40, ChatRole.USER, "네, 접수해 주세요", ChatIntent.REPORT)
    msg(41, ChatRole.ASSISTANT, "신고가 접수됐어요!", None)
    msg(60, ChatRole.USER, "장학금은요?", ChatIntent.INQUIRY)
    msg(62, ChatRole.ASSISTANT, "장학과에 문의해 보세요", ChatIntent.INQUIRY)

    def report(session_id: uuid.UUID | None, created: datetime) -> Report:
        r = Report(category_id=cat.id, priority=Priority.P3, status=ReportStatus.RECEIVED, description="대화 테스트",
                   location_raw="테스트 위치", created_at=created, session_id=session_id)
        db.add(r)
        db.flush()
        return r

    with_chat = report(sess.id, t0 + timedelta(seconds=40))
    no_session = report(None, t0)
    db.commit()
    ids = {"report": with_chat.id, "no_session": no_session.id}
    cat_id, sess_id = cat.id, sess.id
    db.close()
    yield ids
    db = get_sessionmaker()()
    db.execute(delete(Report).where(Report.id.in_(ids.values())))
    db.execute(delete(ChatMessage).where(ChatMessage.session_id == sess_id))
    db.execute(delete(ChatSession).where(ChatSession.id == sess_id))
    db.execute(delete(Category).where(Category.id == cat_id))
    db.commit()
    db.close()


def test_requires_token(client: TestClient) -> None:
    assert client.get(f"{BASE}/{uuid.uuid4()}/conversation").status_code == 401


def test_unknown_report_is_404(client: TestClient, headers: dict[str, str]) -> None:
    assert client.get(f"{BASE}/{uuid.uuid4()}/conversation", headers=headers).status_code == 404


def test_returns_only_this_reports_flow_in_order(client: TestClient, headers: dict[str, str],
                                                 cases: dict[str, uuid.UUID]) -> None:
    res = client.get(f"{BASE}/{cases['report']}/conversation", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["truncated"] is False and body["report_id"] == str(cases["report"])
    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("user", "은주1관 화장실 휴지가 없어요"),
        ("assistant", "몇 층인가요?"),
        ("user", "3층이요"),
        ("assistant", "이대로 접수할까요?"),
        ("user", "네, 접수해 주세요"),
        ("assistant", "신고가 접수됐어요!"),
    ]


def test_does_not_leak_identifiers_or_internal_fields(client: TestClient, headers: dict[str, str],
                                                      cases: dict[str, uuid.UUID]) -> None:
    res = client.get(f"{BASE}/{cases['report']}/conversation", headers=headers)
    body = res.json()
    assert set(body) == {"report_id", "display_no", "messages", "truncated"}
    assert all(set(m) == {"role", "content", "created_at"} for m in body["messages"])
    for banned in ("session_id", "user_identifier", "debug_payload", "intent", "secret", "conv-test-"):
        assert banned not in res.text


def test_report_without_session_returns_empty_messages(client: TestClient, headers: dict[str, str],
                                                       cases: dict[str, uuid.UUID]) -> None:
    res = client.get(f"{BASE}/{cases['no_session']}/conversation", headers=headers)
    assert res.status_code == 200
    assert res.json()["messages"] == [] and res.json()["truncated"] is False


def test_real_chat_flow_report_can_be_read_by_admin(client: TestClient, headers: dict[str, str]) -> None:
    """챗봇으로 실제 접수한 신고의 대화가 구간 규칙(접수 완료 안내부터 intent=신고 구간)대로 읽혀야 한다."""
    sid = client.post("/api/v1/chat/sessions").json()["session_id"]

    def send(content: str, action: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = {"content": content}
        if action:
            payload["action"] = action
        res = client.post(f"/api/v1/chat/sessions/{sid}/messages", json=payload)
        assert res.status_code == 200, res.text
        return res.json()  # type: ignore[no-any-return]

    body = send("혜인관 2층 화장실 물이 계속 새요")
    for _ in range(6):
        if body.get("report_created"):
            break
        body = send("접수", "confirm_report") if body.get("confirm_required") else send("네, 접수해 주세요")
    assert body.get("report_created") is True, body
    report = body["report"]
    assert isinstance(report, dict)
    try:
        res = client.get(f"{BASE}/{report['id']}/conversation", headers=headers)
        assert res.status_code == 200, res.text
        msgs = res.json()["messages"]
        assert msgs[0] == {**msgs[0], "role": "user", "content": "혜인관 2층 화장실 물이 계속 새요"}
        assert msgs[-1]["role"] == "assistant" and f"접수번호는 {report['display_no']}번" in msgs[-1]["content"]
        assert [m["role"] for m in msgs].count("user") >= 2  # 처음 말 + 접수 확인
        assert any(m["content"] == "접수" or "접수" in m["content"] for m in msgs if m["role"] == "user")
        assert res.json()["truncated"] is False
    finally:
        db = get_sessionmaker()()
        db.execute(delete(Report).where(Report.id == uuid.UUID(str(report["id"]))))
        db.execute(delete(ChatMessage).where(ChatMessage.session_id == uuid.UUID(sid)))
        db.execute(delete(ChatSession).where(ChatSession.id == uuid.UUID(sid)))
        db.commit()
        db.close()
