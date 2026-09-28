"""챗봇 API 통합테스트 — 실제 PostgreSQL에서 세션 생성 → 대화 → 신고 접수까지 (작업 1-3).

DB가 필요해서 TEST_DATABASE_URL 환경변수가 있을 때만 실행 (없으면 전부 skip).
마이그레이션(alembic upgrade head)을 마친 빈 테스트용 DB를 가리켜야 함 — 운영 DB 금지!
ai 서비스는 호출하지 않고 가짜 분류기로 대체한다 (Gemini 불필요).
"""
import json
import os
from collections.abc import Iterator

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL 없음 — DB 통합테스트 생략")

if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.models import Report, ReportStatusHistory
from app.models.enums import ReportStatus
from app.routers.chat import get_intent_classifier
from app.services.ai_client import AiServiceError, HistoryItem, IntentResult

# 문장 → 가짜 의도분류 결과 (명세서 4-4 few-shot과 같은 판정)
FAKE: dict[str, IntentResult] = {
    "3동 2층 화장실 물이 계속 새요": IntentResult(intent="report", report_score=95, inquiry_score=5),
    "물이 계속 새요": IntentResult(intent="report", report_score=90, inquiry_score=10),
    "휴학 신청 어떻게 해요?": IntentResult(intent="inquiry", report_score=3, inquiry_score=97),
    "계단이 미끄러운데 어떻게 해야 하나요?": IntentResult(
        intent="unclear", report_score=55, inquiry_score=45,
        clarifying_question="이걸 신고로 접수해드릴까요, 안내가 필요하신 건가요?",
    ),
    "신고해 주세요": IntentResult(intent="report", report_score=90, inquiry_score=10),
    "장애": IntentResult(intent="report", report_score=0, inquiry_score=0),
}


def fake_classifier(text: str, history: list[HistoryItem]) -> IntentResult:
    if text == "장애":
        raise AiServiceError("gemini down")
    return FAKE[text]


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    app.dependency_overrides[get_intent_classifier] = lambda: fake_classifier
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def new_session(client: TestClient) -> str:
    res = client.post("/api/v1/chat/sessions")
    assert res.status_code == 201
    return res.json()["session_id"]


def send(client: TestClient, sid: str, content: str):  # type: ignore[no-untyped-def]
    return client.post(f"/api/v1/chat/sessions/{sid}/messages", json={"content": content})


def test_report_created_in_one_message(client: TestClient) -> None:
    sid = new_session(client)
    res = send(client, sid, "3동 2층 화장실 물이 계속 새요")
    assert res.status_code == 200
    body = res.json()
    assert body["intent"] == "report" and body["report_created"] is True
    r = body["report"]
    assert r["category"]["name"] == "시설·설비"
    assert r["priority"] == "P2"  # 공용공간(고) × 안 급함(저)
    assert r["status"] == "접수"

    db = get_sessionmaker()()
    report = db.scalar(select(Report).where(Report.display_no == r["display_no"]))
    assert report is not None
    assert report.location_raw == "3동 2층 화장실"  # buildings 비어 있어 자유텍스트로 저장
    assert report.floor == "2" and report.detail == "화장실"
    assert report.sla_deadline is not None
    hist = db.scalars(
        select(ReportStatusHistory).where(ReportStatusHistory.report_id == report.id)
    ).all()
    assert [h.to_status for h in hist] == [ReportStatus.RECEIVED]
    db.close()


def test_slot_filling_asks_location_then_creates(client: TestClient) -> None:
    sid = new_session(client)
    first = send(client, sid, "물이 계속 새요").json()
    assert first["intent"] == "report" and "follow_up_question" in first
    assert first["follow_up_question"].startswith("어디에서")
    assert first["slots_filled"]["location"] is None
    # 답변은 의도분류 없이 이어서 처리 (FAKE에 없는 문장이어도 됨)
    second = send(client, sid, "3동 2층 화장실이요").json()
    assert second["report_created"] is True
    # 접수 후 다음 메시지는 새 대화
    third = send(client, sid, "휴학 신청 어떻게 해요?")
    assert third.headers["content-type"].startswith("text/event-stream")


def test_cancel_during_slot_filling(client: TestClient) -> None:
    sid = new_session(client)
    send(client, sid, "물이 계속 새요")
    res = send(client, sid, "취소").json()
    assert "취소" in res["follow_up_question"]
    assert "report_created" not in res


def test_unclear_then_report_keeps_original_sentence(client: TestClient) -> None:
    sid = new_session(client)
    unclear = send(client, sid, "계단이 미끄러운데 어떻게 해야 하나요?").json()
    assert unclear == {
        "intent": "unclear",
        "clarifying_question": "이걸 신고로 접수해드릴까요, 안내가 필요하신 건가요?",
    }
    res = send(client, sid, "신고해 주세요").json()
    assert res["report_created"] is True
    assert res["report"]["category"]["name"] == "안전"
    assert res["report"]["priority"] == "P1"  # 공용공간 × 안전위협


def test_inquiry_returns_sse(client: TestClient) -> None:
    sid = new_session(client)
    res = send(client, sid, "휴학 신청 어떻게 해요?")
    assert res.status_code == 200
    events = [json.loads(line[6:]) for line in res.text.splitlines() if line.startswith("data: ")]
    assert "delta" in events[0]
    assert events[-1] == {"done": True, "sources": []}


def test_ai_failure_returns_503(client: TestClient) -> None:
    sid = new_session(client)
    res = send(client, sid, "장애")
    assert res.status_code == 503
    assert res.json() == {"detail": "잠시 후 다시 시도해주세요"}


def test_unknown_session_404(client: TestClient) -> None:
    res = send(client, "00000000-0000-0000-0000-000000000000", "3동 2층 화장실 물이 계속 새요")
    assert res.status_code == 404
