"""신고 대화 에이전트 API 통합테스트 — 가짜 에이전트로 실제 DB에서 첫 턴 → 되묻기 → 확인 → 접수까지 (작업 1-3f).

TEST_DATABASE_URL이 있을 때만 실행 (운영 DB 금지!). Gemini는 부르지 않는다.
"""
import os
from collections.abc import Iterator
from typing import Any

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
from app.models import Report
from app.routers.chat import get_agent, get_intent_classifier, get_judger
from app.services.ai_client import AgentState, AgentTurn, AiServiceError, IntentResult

SCRIPT: dict[str, Any] = {"turns": [], "calls": [], "fail": False}


def fake_agent(conv, state, prev, left, buildings, candidates, hints):  # type: ignore[no-untyped-def]
    SCRIPT["calls"].append({"conv": conv, "state": state, "prev": prev, "left": left, "candidates": candidates})
    if SCRIPT["fail"]:
        raise AiServiceError("agent down")
    return SCRIPT["turns"].pop(0)


def fake_classifier(text, history):  # type: ignore[no-untyped-def]
    return IntentResult(intent="report", report_score=90, inquiry_score=10)


def fake_judger(text, location, categories):  # type: ignore[no-untyped-def]
    raise AiServiceError("judge down")  # 규칙 기반 판정으로 대체


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    app.dependency_overrides[get_intent_classifier] = lambda: fake_classifier
    app.dependency_overrides[get_judger] = lambda: fake_judger
    app.dependency_overrides[get_agent] = lambda: fake_agent
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def reset_script() -> None:
    SCRIPT.update(turns=[], calls=[], fail=False)


def turn(action: str, message: str, choices: list[str] | None = None, **state: Any) -> AgentTurn:
    base: dict[str, Any] = {"problem": "불이 안 켜져요", "problem_clear": True}
    base.update(state)
    return AgentTurn(action=action, message=message, choices=choices or [], state=AgentState(**base))  # type: ignore[arg-type]


def new_session(client: TestClient) -> str:
    return client.post("/api/v1/chat/sessions").json()["session_id"]  # type: ignore[no-any-return]


def send(client: TestClient, sid: str, content: str, action: str | None = None) -> Any:
    body: dict[str, Any] = {"content": content}
    if action:
        body["action"] = action
    return client.post(f"/api/v1/chat/sessions/{sid}/messages", json=body)


def test_professor_office_goes_straight_to_confirm_and_submits(client: TestClient) -> None:
    SCRIPT["turns"] = [
        turn("confirm", "북악관 6층 606호 장문수 교수연구실의 불이 안 켜지는 문제예요. 이대로 접수할까요?",
             building="북악관", floor="6", room_no="606", room_name="장문수 교수연구실", location_certainty="confirmed")
    ]
    sid = new_session(client)
    first = send(client, sid, "장문수 교수실 불이 안 켜져").json()
    assert first["confirm_required"] and "606" in first["summary"]
    assert any("북악관" in str(c["candidates"]) for c in SCRIPT["calls"])  # 학교 데이터 후보를 건넴
    done = send(client, sid, "네, 접수해 주세요").json()  # 확인 뒤의 긍정은 에이전트 없이 접수
    assert "report" in done and len(SCRIPT["calls"]) == 1
    db = get_sessionmaker()()
    try:
        report = db.scalar(select(Report).where(Report.id == done["report"]["id"]))
        assert report is not None and report.floor == "6" and "606" in (report.detail or "")
    finally:
        db.close()


def test_question_then_answer_then_confirm(client: TestClient) -> None:
    SCRIPT["turns"] = [
        turn("ask", "어떤 문제인지 알려주시겠어요?", problem="", problem_clear=False),
        turn("confirm", "청운관 3층 정수기에서 물이 안 나오는 문제예요. 접수할까요?", building="청운관", floor="3",
             place="정수기", problem="물이 안 나와요"),
    ]
    sid = new_session(client)
    ask = send(client, sid, "청운관 3층 정수기").json()
    assert ask["follow_up_question"] and "confirm_required" not in ask
    confirm = send(client, sid, "물이 안 나와요").json()
    assert confirm["confirm_required"]
    assert SCRIPT["calls"][1]["prev"] == "ask" and SCRIPT["calls"][1]["left"] == 1


def test_agent_submit_without_confirm_is_downgraded(client: TestClient) -> None:
    SCRIPT["turns"] = [turn("submit", "", building="청운관", floor="3", place="정수기")]
    sid = new_session(client)
    res = send(client, sid, "청운관 3층 정수기에서 물이 안 나와요").json()
    assert res["confirm_required"]  # 바로 접수되지 않고 확인 단계를 거침


def test_decline_does_not_start_flow_and_retry_keeps_context(client: TestClient) -> None:
    SCRIPT["turns"] = [
        turn("decline", "그건 신고로 접수하기 어려워 보여요. 다른 시설 문제가 있으면 말씀해 주세요.", plausible=False),
        turn("confirm", "정수기에서 커피가 나오는 문제로 담당자 확인용으로 접수할까요?", place="정수기",
             problem="커피가 나와요", plausible=False, staff_check=["내용 확인"]),
    ]
    sid = new_session(client)
    res = send(client, sid, "정수기에서 커피가 나와요")
    assert res.headers["content-type"].startswith("text/event-stream")
    second = send(client, sid, "진짜예요").json()
    assert second["confirm_required"]
    convo = SCRIPT["calls"][1]["conv"]
    assert [m.content for m in convo][:2] == ["정수기에서 커피가 나와요", "그건 신고로 접수하기 어려워 보여요. 다른 시설 문제가 있으면 말씀해 주세요."]


def test_agent_failure_falls_back_to_rule_flow(client: TestClient) -> None:
    SCRIPT["fail"] = True
    sid = new_session(client)
    res = send(client, sid, "혜인관 2층 화장실 물이 계속 새요").json()
    assert res["intent"] == "report" and res["choices"] == ["네, 접수해 주세요", "아니요, 안내만 받을게요"]


def test_cancel_button_ends_agent_flow(client: TestClient) -> None:
    SCRIPT["turns"] = [turn("ask", "몇 층인가요?")]
    sid = new_session(client)
    send(client, sid, "청운관 정수기 고장")
    res = send(client, sid, "취소할게요", action="cancel_report").json()
    assert res["report_cancelled"]


def test_pending_issue_is_mentioned_after_submit(client: TestClient) -> None:
    SCRIPT["turns"] = [
        turn("confirm", "청운관 3층 정수기 건으로 접수할까요?", building="청운관", floor="3", place="정수기",
             pending_issues=["북악관 엘리베이터"])
    ]
    sid = new_session(client)
    send(client, sid, "청운관 3층 정수기 고장, 그리고 북악관 엘리베이터도 이상해요")
    done = send(client, sid, "네").json()
    assert "북악관 엘리베이터" in done["message"]
