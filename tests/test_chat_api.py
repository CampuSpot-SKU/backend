"""챗봇 API 통합테스트 — 실제 PostgreSQL에서 세션 생성 → 대화 → 요약 확인 → 신고 접수까지 (작업 1-3, 1-3c).

DB가 필요해서 TEST_DATABASE_URL 환경변수가 있을 때만 실행 (없으면 전부 skip).
마이그레이션(alembic upgrade head)을 마친 빈 테스트용 DB를 가리켜야 함 — 운영 DB 금지!
ai 서비스는 호출하지 않고 가짜 분류기로 대체한다 (Gemini 불필요).
"""
import json
import os
from collections.abc import Iterator
from typing import Any

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL 없음 — DB 통합테스트 생략")

if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.models import Building, Report, ReportStatusHistory
from app.models.enums import ReportStatus
from app.routers.chat import get_intent_classifier, get_phraser
from app.services.ai_client import AiServiceError, HistoryItem, IntentResult

# 문장 → 가짜 의도분류 결과 (명세서 4-4 few-shot과 같은 판정)
FAKE: dict[str, IntentResult] = {
    "혜인관 2층 화장실 물이 계속 새요": IntentResult(intent="report", report_score=95, inquiry_score=5),
    "물이 계속 새요": IntentResult(intent="report", report_score=90, inquiry_score=10),
    "휴학 신청 어떻게 해요?": IntentResult(intent="inquiry", report_score=3, inquiry_score=97),
    "계단이 미끄러운데 어떻게 해야 하나요?": IntentResult(
        intent="unclear", report_score=55, inquiry_score=45,
        clarifying_question="이걸 신고로 접수해드릴까요, 안내가 필요하신 건가요?",
    ),
    "신고해 주세요": IntentResult(intent="report", report_score=90, inquiry_score=10),
    "장애": IntentResult(intent="report", report_score=0, inquiry_score=0),
    "3동 2층 화장실 물이 계속 새요": IntentResult(intent="report", report_score=95, inquiry_score=5),
    "공학관 301호 프로젝터가 안 켜져요": IntentResult(intent="report", report_score=90, inquiry_score=10),
    "은주관 3층 화장실 물이 새요": IntentResult(intent="report", report_score=92, inquiry_score=8),
    "대일관 4층 복도 조명이 깜빡거려요": IntentResult(intent="report", report_score=90, inquiry_score=10),
    "강의실 와이파이가 안 터져요": IntentResult(intent="report", report_score=88, inquiry_score=12),
    "안녕": IntentResult(intent="chitchat", report_score=0, inquiry_score=0, talk="greeting"),
    "날씨가 좋네": IntentResult(intent="chitchat", report_score=0, inquiry_score=0, talk="smalltalk"),
    "오늘 날씨 알려줘": IntentResult(intent="off_topic", report_score=0, inquiry_score=0, talk="off_topic"),
    "햄버거 만드는 법": IntentResult(intent="off_topic", report_score=0, inquiry_score=0, talk="off_topic"),
    "파이썬 코드 짜줘": IntentResult(intent="off_topic", report_score=0, inquiry_score=0, talk="off_topic"),
    "4층이 이상해": IntentResult(intent="report", report_score=80, inquiry_score=20),
    "4층 에어컨이 안나와요": IntentResult(intent="report", report_score=90, inquiry_score=10),
    "3층 정수기가 고장났어요": IntentResult(intent="report", report_score=90, inquiry_score=10),
    "혜인관 7층 화장실 물이 새요": IntentResult(intent="report", report_score=93, inquiry_score=7),
    "스포렉스 샤워실 온수가 안 나와요": IntentResult(intent="report", report_score=90, inquiry_score=10),
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


def send(  # type: ignore[no-untyped-def]
    client: TestClient, sid: str, content: str, action: str | None = None,
    draft: dict[str, str] | None = None,
):
    payload: dict[str, object] = {"content": content}
    if action:
        payload["action"] = action
    if draft is not None:
        payload["draft"] = draft
    return client.post(f"/api/v1/chat/sessions/{sid}/messages", json=payload)


def begin(client: TestClient, sid: str, text: str) -> dict[str, Any]:
    """신고 문장을 보내 접수 제안을 받고, "네, 접수해 주세요"로 수락한 뒤의 응답 (대화형 흐름 4-1)."""
    offer = send(client, sid, text).json()
    assert offer["intent"] == "report" and "confirm_required" not in offer, offer
    assert offer["choices"] == ["네, 접수해 주세요", "아니요, 안내만 받을게요"]
    return send(client, sid, "네, 접수해 주세요").json()  # type: ignore[no-any-return]


def report_count() -> int:
    db = get_sessionmaker()()
    try:
        return db.scalar(select(func.count()).select_from(Report)) or 0
    finally:
        db.close()


def get_report(display_no: int) -> Report:
    db = get_sessionmaker()()
    try:
        report = db.scalar(select(Report).where(Report.display_no == display_no))
        assert report is not None
        db.expunge(report)
        return report
    finally:
        db.close()


def building_id(name: str):  # type: ignore[no-untyped-def]
    db = get_sessionmaker()()
    try:
        return db.scalar(select(Building.id).where(Building.name == name))
    finally:
        db.close()


def sse_text(res) -> str:  # type: ignore[no-untyped-def]
    events = [json.loads(line[6:]) for line in res.text.splitlines() if line.startswith("data: ")]
    return "".join(e.get("delta", "") for e in events)


# ── 요약 확인 후 접수 (신고 흐름 개편 4-1) ────────────────────────────────────
def test_summary_then_confirm_by_text(client: TestClient) -> None:
    sid = new_session(client)
    before = report_count()
    body = begin(client, sid, "혜인관 2층 화장실 물이 계속 새요")
    # 슬롯이 다 차도 바로 접수하지 않고 요약 확인
    assert body["intent"] == "report" and body["confirm_required"] is True
    assert "report_created" not in body and report_count() == before
    assert "접수할까요?" in body["summary"] and "혜인관 2층 화장실에서" in body["summary"]
    assert "위치:" not in body["summary"] and "상황:" not in body["summary"]  # 목록 형식 아님
    assert body["choices"] == ["네, 접수해 주세요", "내용을 고칠래요", "취소할게요"]
    assert body["follow_up_question"] == body["summary"]  # 하위 호환
    assert body["slots_filled"]["building"] == "혜인관"
    assert body["slots_filled"]["floor"] == "2"
    assert body["slots_filled"]["detail"] == "화장실"
    assert body["slots_filled"]["category"] == "시설·설비"

    # 버튼이 없는 화면: "접수"라고 입력해도 접수
    done = send(client, sid, "접수").json()
    assert done["intent"] == "report" and done["report_created"] is True
    r = done["report"]
    assert r["category"]["name"] == "시설·설비"
    assert r["priority"] == "P2"  # 공용공간(고) × 안 급함(저)
    assert r["status"] == "접수"
    assert report_count() == before + 1
    assert f"접수번호는 {r['display_no']}번" in done["message"]
    assert "혜인관 2층 화장실에서 생긴" in done["message"] and "P2로 판단했어요" in done["message"]
    assert "위치:" not in done["message"]

    db = get_sessionmaker()()
    report = db.scalar(select(Report).where(Report.display_no == r["display_no"]))
    assert report is not None
    assert report.building_id == building_id("혜인관")  # 공식 건물 목록과 매칭돼 건물 ID로 저장
    assert report.location_raw is None
    assert report.floor == "2" and report.detail == "화장실"
    assert report.sla_deadline is not None
    hist = db.scalars(
        select(ReportStatusHistory).where(ReportStatusHistory.report_id == report.id)
    ).all()
    assert [h.to_status for h in hist] == [ReportStatus.RECEIVED]
    db.close()


def test_confirm_button_uses_final_form_values(client: TestClient) -> None:
    sid = new_session(client)
    body = begin(client, sid, "혜인관 2층 화장실 물이 계속 새요")
    assert body["confirm_required"] is True
    form = {"building": "혜인관", "floor": "7", "detail": "701호", "description": "화장실 물이 새요"}
    done = send(client, sid, "접수", action="confirm_report", draft=form).json()
    assert done["report_created"] is True
    report = get_report(done["report"]["display_no"])
    assert report.building_id == building_id("혜인관")  # 폼의 건물 이름 → 건물 목록과 매칭
    assert report.floor == "7" and report.detail == "701호"
    assert report.location_raw is None
    assert "혜인관 7층 701호에서 생긴" in done["message"]


def test_form_with_custom_building_text_is_kept_as_raw(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "혜인관 2층 화장실 물이 계속 새요")
    form = {"building": "체육관 옆 창고", "floor": "", "detail": "", "description": ""}
    done = send(client, sid, "접수", action="confirm_report", draft=form).json()
    assert done["report_created"] is True
    report = get_report(done["report"]["display_no"])
    assert report.building_id is None and report.location_raw == "체육관 옆 창고"


def test_slot_filling_asks_location_then_summary_then_creates(client: TestClient) -> None:
    sid = new_session(client)
    first = begin(client, sid, "물이 계속 새요")
    assert first["intent"] == "report" and "confirm_required" not in first
    assert first["follow_up_question"].startswith("필요한 정보를 물어볼게요. 어디에서")
    assert first["slots_filled"]["location"] is None
    assert first["slots_filled"]["building"] is None  # 1-3c 형식: 필드는 항상 있음(값이 null)
    assert first["choices"][:2] == ["은주1관", "은주2관"]  # 추천 답변 (직접 입력도 가능)
    # 답변은 의도분류 없이 이어서 처리 (FAKE에 없는 문장이어도 됨)
    second = send(client, sid, "혜인관 2층 화장실이요").json()
    assert second["confirm_required"] is True
    third = send(client, sid, "접수", action="confirm_report").json()
    assert third["report_created"] is True
    # 접수 후 다음 메시지는 새 대화
    fourth = send(client, sid, "휴학 신청 어떻게 해요?")
    assert fourth.headers["content-type"].startswith("text/event-stream")


def test_correction_in_summary_updates_and_summarizes_again(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "혜인관 2층 화장실 물이 계속 새요")
    again = send(client, sid, "아니 3층이에요").json()
    assert again["confirm_required"] is True
    assert again["slots_filled"]["floor"] == "3"  # 나중에 말한 층이 우선
    done = send(client, sid, "네").json()
    assert done["report_created"] is True
    assert get_report(done["report"]["display_no"]).floor == "3"


def test_cancel_during_slot_filling(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "물이 계속 새요")
    res = send(client, sid, "취소").json()
    assert res["report_cancelled"] is True and "취소" in res["message"]
    assert res["follow_up_question"] == res["message"]
    assert "report_created" not in res
    # 취소 뒤 다음 메시지는 새 대화
    assert send(client, sid, "휴학 신청 어떻게 해요?").headers["content-type"].startswith(
        "text/event-stream"
    )


def test_cancel_button_at_summary_creates_nothing(client: TestClient) -> None:
    sid = new_session(client)
    before = report_count()
    begin(client, sid, "혜인관 2층 화장실 물이 계속 새요")
    res = send(client, sid, "취소", action="cancel_report").json()
    assert res["report_cancelled"] is True
    assert report_count() == before


def test_switch_to_inquiry_ends_report_flow(client: TestClient) -> None:
    sid = new_session(client)
    before = report_count()
    assert begin(client, sid, "물이 계속 새요")["intent"] == "report"
    res = send(client, sid, "안내만 받을래요", action="switch_to_inquiry")
    assert res.headers["content-type"].startswith("text/event-stream")
    assert sse_text(res)
    # 신고 흐름이 끝났으므로 다음 신고 문장은 새로 시작 (이전 되묻기에 대한 답으로 안 먹힘)
    nxt = begin(client, sid, "혜인관 2층 화장실 물이 계속 새요")
    assert nxt["confirm_required"] is True
    assert report_count() == before


def test_switch_to_inquiry_by_text_during_flow(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "물이 계속 새요")
    res = send(client, sid, "안내만 받을래요")
    assert res.headers["content-type"].startswith("text/event-stream")


# ── 상황(description)에 위치 답변·질문이 섞이지 않음 ────────────────────────────
def test_location_answer_is_not_added_to_description(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "강의실 와이파이가 안 터져요")
    assert "몇 호 강의실인가요?" in send(client, sid, "혜인관 3층이요").json()["follow_up_question"]
    second = send(client, sid, "301호요").json()
    assert second["confirm_required"] is True
    assert second["slots_filled"]["description"] == "강의실 와이파이가 안 터져요"
    assert "'강의실 와이파이가 안 터져요'" in second["summary"]  # "혜인관 3층이요"가 안 붙음
    done = send(client, sid, "접수").json()
    report = get_report(done["report"]["display_no"])
    assert report.description == "강의실 와이파이가 안 터져요"
    assert report.floor == "3"  # 위치는 답변에서 그대로 반영


def test_question_reply_to_location_ask_is_ignored(client: TestClient) -> None:
    sid = new_session(client)
    first = begin(client, sid, "3층 정수기가 고장났어요")
    assert first["follow_up_question"].startswith("필요한 정보를 물어볼게요. 어느 건물 3층인가요?")
    second = send(client, sid, "3동이 우리학교에 있어?").json()
    # 질문은 위치로도 상황으로도 쓰지 않음 (없는 건물 "3동"이 채워지면 안 됨) → 건물을 아직 모르니 다시 물음
    assert "confirm_required" not in second
    assert "어느 건물의 3층" in second["follow_up_question"]
    assert second["slots_filled"]["building"] is None
    assert second["slots_filled"]["floor"] == "3"
    assert second["slots_filled"]["description"] == "3층 정수기가 고장났어요"
    third = send(client, sid, "잘 모르겠어요").json()  # 모른다고 하면 더 묻지 않고 확인으로
    assert third["confirm_required"] is True
    assert "우리학교" not in third["summary"]


def test_floor_only_answer_asks_building_again(client: TestClient) -> None:
    """건물을 물었는데 층만 답하면, 부족한 건물을 다시 물음 ("3층"만 말하고 접수 확인으로 가면 안 됨)."""
    sid = new_session(client)
    begin(client, sid, "강의실 와이파이가 안 터져요")
    again = send(client, sid, "3층").json()
    assert "confirm_required" not in again
    assert "어느 건물의 3층 강의실인가요?" in again["follow_up_question"]
    room = send(client, sid, "북악관").json()
    assert "몇 층" not in room["follow_up_question"]  # 층은 이미 앎
    assert "몇 호 강의실인가요?" in room["follow_up_question"]
    summary = send(client, sid, "301호").json()
    assert summary["confirm_required"] is True
    assert summary["slots_filled"]["building"] == "북악관" and summary["slots_filled"]["floor"] == "3"


def test_unsure_location_is_not_asked_forever(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "강의실 와이파이가 안 터져요")
    send(client, sid, "글쎄요")  # 건물을 못 알아들음 → 한 번 더
    assert send(client, sid, "몰라요").json()["confirm_required"] is True


def test_unknown_building_is_not_accepted_as_building(client: TestClient) -> None:
    sid = new_session(client)
    first = begin(client, sid, "3동 2층 화장실 물이 계속 새요")
    # 우리 학교에 없는 "3동"은 건물로 받지 않고, 공식 목록을 보여주며 되물음
    assert "confirm_required" not in first
    assert "'3동'은(는) 학교 건물 목록에 없어요" in first["follow_up_question"]
    assert "혜인관" in first["follow_up_question"]
    assert first["slots_filled"]["building"] is None
    second = send(client, sid, "혜인관이요").json()
    assert second["confirm_required"] is True
    assert second["slots_filled"]["building"] == "혜인관"
    assert second["slots_filled"]["floor"] == "2"
    done = send(client, sid, "접수").json()
    report = get_report(done["report"]["display_no"])
    assert report.building_id == building_id("혜인관")


def test_unknown_building_still_unknown_after_one_ask_is_flagged(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "공학관 301호 프로젝터가 안 켜져요")  # 공학관은 학교에 없음 → 되묻기
    second = send(client, sid, "모르겠어요").json()
    assert second["confirm_required"] is True  # 되묻기는 1번뿐
    assert second["slots_filled"]["building"] is None  # 건물로는 절대 안 채움
    assert "학교 건물 목록에 없어서" in second["summary"]


def test_location_answer_with_problem_text_is_kept_in_description(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "물이 계속 새요")
    second = send(client, sid, "혜인관 3층 화장실이요, 변기가 막혔어요").json()
    assert second["confirm_required"] is True
    assert "변기가 막혔어요" in second["slots_filled"]["description"]


# ── 접수 직후의 인사 (명세 4-1 보완 ②) ────────────────────────────────────────
def test_thanks_after_report_is_not_a_new_report(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "혜인관 2층 화장실 물이 계속 새요")
    assert send(client, sid, "접수").json()["report_created"] is True
    before = report_count()
    res = send(client, sid, "고마워요")  # FAKE 분류기에 없는 문장 — 분류기를 부르면 KeyError
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert "다행" in sse_text(res)
    assert report_count() == before
    # 인사 뒤의 진짜 신고는 새로 시작
    assert send(client, sid, "물이 계속 새요").json()["intent"] == "report"


# ── 위치 처리 규칙 (명세 4-1) ─────────────────────────────────────────────────
def test_eunju_asks_which_building_with_choices(client: TestClient) -> None:
    sid = new_session(client)
    first = begin(client, sid, "은주관 3층 화장실 물이 새요")
    assert "은주1관인가요, 은주2관인가요?" in first["follow_up_question"]
    assert first["choices"] == ["은주1관", "은주2관", "잘 모르겠어요"]
    assert first["slots_filled"]["building"] == "은주관(1·2관 미확정)"
    second = send(client, sid, "은주1관이요").json()
    assert second["confirm_required"] is True
    assert second["slots_filled"]["building"] == "은주1관"
    done = send(client, sid, "접수").json()
    report = get_report(done["report"]["display_no"])
    assert report.building_id == building_id("은주1관") and report.floor == "3"


def test_eunju_button_answer_one_gwan(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "은주관 3층 화장실 물이 새요")
    second = send(client, sid, "2관").json()
    assert second["slots_filled"]["building"] == "은주2관"


def test_eunju_unsure_keeps_raw_location(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "은주관 3층 화장실 물이 새요")
    second = send(client, sid, "잘 모르겠어요").json()
    assert second["confirm_required"] is True  # 위치 되묻기는 1번뿐 — 더 묻지 않음
    done = send(client, sid, "접수").json()
    report = get_report(done["report"]["display_no"])
    assert report.building_id is None
    assert report.location_raw is not None and "은주관(1·2관 미확정)" in report.location_raw


def test_fourth_floor_in_building_without_one_asks_once(client: TestClient) -> None:
    sid = new_session(client)
    first = begin(client, sid, "대일관 4층 복도 조명이 깜빡거려요")
    assert "대일관에는 4층이 없는 걸로 알고 있어요" in first["follow_up_question"]
    assert "4층이 맞아요" in first["choices"] and "3층" in first["choices"]
    assert "있는 층은" in first["follow_up_question"]  # 학교 데이터의 실제 층을 알려줌
    second = send(client, sid, "아 3층이에요").json()
    assert second["confirm_required"] is True
    assert second["slots_filled"]["floor"] == "3"


def test_fourth_floor_confirmed_is_kept(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "대일관 4층 복도 조명이 깜빡거려요")
    second = send(client, sid, "네 4층 맞아요").json()
    assert second["confirm_required"] is True and second["slots_filled"]["floor"] == "4"


def test_generic_place_name_only_asks_for_building(client: TestClient) -> None:
    sid = new_session(client)
    first = begin(client, sid, "강의실 와이파이가 안 터져요")
    assert "어느 건물 몇 층 강의실인가요?" in first["follow_up_question"]
    room = send(client, sid, "혜인관 3층이요").json()
    assert "몇 호 강의실인가요?" in room["follow_up_question"]  # 강의실은 여러 개 — 어느 방인지
    assert "복도" in room["choices"] and "화장실" in room["choices"]  # 방이 아닌 장소도 고를 수 있음
    assert "301호 강의실" in room["choices"]  # 그 층에 실제 있는 강의실을 고르게 함
    second = send(client, sid, "301호").json()
    assert second["confirm_required"] is True
    assert second["slots_filled"]["building"] == "혜인관"
    assert second["slots_filled"]["detail"] == "301호 강의실"


def test_floor_only_asks_location_once_then_summarizes(client: TestClient) -> None:
    sid = new_session(client)
    first = begin(client, sid, "3층 정수기가 고장났어요")
    assert first["follow_up_question"].startswith("필요한 정보를 물어볼게요. 어느 건물 3층인가요?")
    second = send(client, sid, "모르겠어요").json()
    assert second["confirm_required"] is True  # 같은 질문은 1번만
    assert second["slots_filled"]["building"] is None


def test_facility_name_fills_building(client: TestClient) -> None:
    sid = new_session(client)
    body = begin(client, sid, "스포렉스 샤워실 온수가 안 나와요")
    assert body["confirm_required"] is True  # 특정 시설 이름은 위치로 인정 — 건물을 안 묻고
    assert body["slots_filled"]["building"] == "유담관"
    assert body["slots_filled"]["floor"] == "3"
    assert "스포렉스" in body["slots_filled"]["detail"]
    done = send(client, sid, "접수").json()
    assert get_report(done["report"]["display_no"]).building_id == building_id("유담관")


def test_building_table_match_in_chat(client: TestClient) -> None:
    sid = new_session(client)
    body = begin(client, sid, "혜인관 7층 화장실 물이 새요")
    assert body["confirm_required"] is True and body["slots_filled"]["building"] == "혜인관"
    done = send(client, sid, "접수").json()
    report = get_report(done["report"]["display_no"])
    assert report.building_id == building_id("혜인관") and report.location_raw is None


def test_unclear_then_report_keeps_original_sentence(client: TestClient) -> None:
    sid = new_session(client)
    unclear = send(client, sid, "계단이 미끄러운데 어떻게 해야 하나요?").json()
    assert unclear == {
        "intent": "unclear",
        "clarifying_question": "이걸 신고로 접수해드릴까요, 안내가 필요하신 건가요?",
    }
    res = begin(client, sid, "신고해 주세요")
    # 원래 문장(계단이 미끄러운데)이 신고 내용에 들어 있어 상황은 안 묻고, 계단만으론 건물을 모름 → 위치 되묻기
    assert "어느 건물 몇 층 계단인가요?" in res["follow_up_question"]
    res = send(client, sid, "혜인관 2층이요").json()
    assert res["confirm_required"] is True
    done = send(client, sid, "접수").json()
    assert done["report_created"] is True
    assert done["report"]["category"]["name"] == "안전"
    assert done["report"]["priority"] == "P1"  # 공용공간 × 안전위협


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
    res = send(client, "00000000-0000-0000-0000-000000000000", "혜인관 2층 화장실 물이 계속 새요")
    assert res.status_code == 404


def test_stale_confirm_without_flow_starts_from_form(client: TestClient) -> None:
    sid = new_session(client)
    before = report_count()
    res = send(client, sid, "접수", action="confirm_report",
               draft={"building": "", "floor": "", "detail": "", "description": ""}).json()
    assert res["intent"] == "report" and "report_created" not in res  # 서버에 신고가 없으면 접수 안 함
    assert report_count() == before


# ── 페이지를 새로 열면 진행 중이던 신고 흐름이 끝남 (새로고침 문제) ─────────────────────
def reset(client: TestClient, sid: str):  # type: ignore[no-untyped-def]
    return client.post(f"/api/v1/chat/sessions/{sid}/reset")


def test_reset_ends_in_progress_flow_so_old_input_does_not_leak(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "물이 계속 새요")  # 위치를 되묻는 중에 페이지를 새로 엶
    assert reset(client, sid).status_code == 204
    body = begin(client, sid, "혜인관 2층 화장실 물이 계속 새요")
    assert body["confirm_required"] is True
    # 예전 입력("물이 계속 새요")이 상황에 섞이지 않음
    assert body["slots_filled"]["description"] == "혜인관 2층 화장실 물이 계속 새요"


def test_reset_ends_flow_at_summary_stage(client: TestClient) -> None:
    sid = new_session(client)
    before = report_count()
    assert begin(client, sid, "혜인관 2층 화장실 물이 계속 새요")["confirm_required"] is True
    assert reset(client, sid).status_code == 204
    # 요약이 떠 있던 상태였어도 새 입력은 정정이 아니라 새 신고 — 접수는 만들어지지 않음
    body = send(client, sid, "휴학 신청 어떻게 해요?")
    assert body.headers["content-type"].startswith("text/event-stream")
    assert report_count() == before


def test_reset_without_flow_is_noop(client: TestClient) -> None:
    sid = new_session(client)
    assert reset(client, sid).status_code == 204  # 대화가 없어도 오류 아님
    send(client, sid, "휴학 신청 어떻게 해요?")
    assert reset(client, sid).status_code == 204
    assert reset(client, sid).status_code == 204


def test_reset_unknown_session_404(client: TestClient) -> None:
    assert reset(client, "00000000-0000-0000-0000-000000000000").status_code == 404


def test_locations_endpoint(client: TestClient) -> None:
    res = client.get("/api/v1/locations")
    assert res.status_code == 200
    buildings = res.json()["buildings"]
    names = [b["name"] for b in buildings if "name" in b]
    assert "은주1관" in names and "은주2관" in names
    assert buildings[-1]["custom"] is True  # 단계마다 맨 끝은 "목록에 없음 (직접 입력)"
    first_floor = buildings[0]["floors"][0]
    assert first_floor["places"][-1]["custom"] is True


# ── 대화형 흐름: 접수 제안 → 응 → 정보 묻기 → 문장 확인 (명세 4-1) ─────────────────
def test_offer_comes_first_and_is_conversational(client: TestClient) -> None:
    sid = new_session(client)
    before = report_count()
    offer = send(client, sid, "3층 정수기가 고장났어요").json()
    assert offer["intent"] == "report" and "confirm_required" not in offer
    assert "접수를 도와드릴까요?" in offer["follow_up_question"]
    assert "3층 정수기가 고장났어요" in offer["follow_up_question"]
    assert "위치:" not in offer["follow_up_question"] and "\n" not in offer["follow_up_question"]
    assert offer["choices"] == ["네, 접수해 주세요", "아니요, 안내만 받을게요"]
    # "응" → 필요한 정보를 물어봄 (위치 되묻기)
    ask = send(client, sid, "응").json()
    assert ask["follow_up_question"].startswith("필요한 정보를 물어볼게요.")
    assert report_count() == before


def test_offer_decline_ends_flow(client: TestClient) -> None:
    sid = new_session(client)
    before = report_count()
    send(client, sid, "3층 정수기가 고장났어요")
    res = send(client, sid, "아니요").json()
    assert res["report_cancelled"] is True and "접수는 하지 않을게요" in res["message"]
    assert report_count() == before
    assert send(client, sid, "휴학 신청 어떻게 해요?").headers["content-type"].startswith(
        "text/event-stream"
    )


def test_offer_chip_inquiry_goes_to_inquiry(client: TestClient) -> None:
    sid = new_session(client)
    send(client, sid, "3층 정수기가 고장났어요")
    res = send(client, sid, "아니요, 안내만 받을게요")
    assert res.headers["content-type"].startswith("text/event-stream")


def test_offer_reply_with_info_skips_yes_and_continues(client: TestClient) -> None:
    """"응" 대신 바로 위치를 말해도 수락으로 보고 이어감."""
    sid = new_session(client)
    send(client, sid, "3층 정수기가 고장났어요")
    ask = send(client, sid, "혜인관이에요").json()
    assert "어느 호실인가요?" in ask["follow_up_question"]  # 정수기가 어디 있는지 (복도·로비 등)
    body = send(client, sid, "복도 쪽이에요").json()
    assert body["confirm_required"] is True
    assert body["slots_filled"]["building"] == "혜인관" and body["slots_filled"]["floor"] == "3"
    assert body["slots_filled"]["detail"] == "복도"


def test_edit_chip_asks_what_to_fix_then_summarizes_again(client: TestClient) -> None:
    sid = new_session(client)
    assert begin(client, sid, "혜인관 2층 화장실 물이 계속 새요")["confirm_required"] is True
    ask = send(client, sid, "내용을 고칠래요").json()
    assert "어느 부분을 고칠까요?" in ask["follow_up_question"] and "confirm_required" not in ask
    again = send(client, sid, "3층이에요").json()
    assert again["confirm_required"] is True and again["slots_filled"]["floor"] == "3"
    done = send(client, sid, "네, 접수해 주세요").json()
    assert get_report(done["report"]["display_no"]).floor == "3"


def test_rephrased_messages_keep_flow_state(client: TestClient) -> None:
    """Gemini가 말투를 완전히 바꿔도 단계(kind)는 저장된 값으로 알아봄."""
    def rewrite(kind: str, base: str, must: list[str], history: list[HistoryItem]) -> str:
        return f"[{kind}] " + " ".join(must) if must else f"[{kind}] 음, 조금 더 말씀해 주세요"

    app.dependency_overrides[get_phraser] = lambda: rewrite
    try:
        sid = new_session(client)
        before = report_count()
        assert send(client, sid, "혜인관 2층 화장실 물이 계속 새요").json()["follow_up_question"].startswith("[offer]")
        summary = send(client, sid, "응").json()
        assert summary["confirm_required"] is True and summary["summary"].startswith("[summary]")
        assert send(client, sid, "접수").json()["report_created"] is True
        assert report_count() == before + 1
    finally:
        app.dependency_overrides.pop(get_phraser, None)


def test_phraser_failure_falls_back_to_fixed_text(client: TestClient) -> None:
    """ai 서비스가 없거나 실패하면(테스트 환경은 AI_SERVICE_URL 없음) 고정 문구로 계속 진행."""
    sid = new_session(client)
    offer = send(client, sid, "3층 정수기가 고장났어요").json()
    assert offer["follow_up_question"].endswith("접수를 도와드릴까요?")


def test_building_only_answer_asks_floor_and_room_once(client: TestClient) -> None:
    """"북악관"만 답하면 바로 접수 확인으로 가지 않고 층·호수를 한 번 더 물음."""
    sid = new_session(client)
    ask = begin(client, sid, "강의실 와이파이가 안 터져요")
    assert "어느 건물 몇 층 강의실인가요?" in ask["follow_up_question"]
    floor_ask = send(client, sid, "북악관").json()
    assert "confirm_required" not in floor_ask
    assert "북악관의 몇 층, 몇 호인가요?" in floor_ask["follow_up_question"]
    assert floor_ask["choices"][-1] == "잘 모르겠어요"
    summary = send(client, sid, "3층 301호요").json()
    assert summary["confirm_required"] is True
    assert summary["slots_filled"]["floor"] == "3"


def test_unsure_floor_answer_does_not_ask_again(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "강의실 와이파이가 안 터져요")
    send(client, sid, "북악관")
    room = send(client, sid, "잘 모르겠어요").json()  # 층을 모른다고 해도 어느 방인지는 물음
    assert "몇 호 강의실인가요?" in room["follow_up_question"]
    summary = send(client, sid, "몰라요").json()
    assert summary["confirm_required"] is True  # 모른다고 하면 더 안 물음


def test_screenshot_scenario_abbreviation_fourth_floor_and_room(client: TestClient) -> None:
    """실제 화면 대화: "4층" → "북악"(약칭) → 4층 없는 건물 확인 → 층 정정 → 호수 → 확인 → 접수."""
    sid = new_session(client)
    begin(client, sid, "강의실 와이파이가 안 터져요")
    building_ask = send(client, sid, "4층").json()
    assert "어느 건물의 4층 강의실인가요?" in building_ask["follow_up_question"]
    floor4 = send(client, sid, "북악").json()  # "북악" → 북악관
    assert floor4["slots_filled"]["building"] == "북악관"
    assert "북악관에는 4층이 없는 걸로 알고 있어요" in floor4["follow_up_question"]
    room = send(client, sid, "3층이요").json()
    assert room["slots_filled"]["floor"] == "3"
    assert "몇 호 강의실인가요?" in room["follow_up_question"]
    summary = send(client, sid, "301호").json()
    assert summary["confirm_required"] is True
    assert "북악관 3층 301호" in summary["summary"]
    done = send(client, sid, "네, 접수해 주세요").json()
    report = get_report(done["report"]["display_no"])
    assert report.floor == "3" and (report.detail or "").startswith("301호")
    assert report.building_id == building_id("북악관")


def test_fourth_floor_yes_keeps_it_and_vague_reply_asks_once_more(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "대일관 4층 복도 조명이 깜빡거려요")
    again = send(client, sid, "어").json()  # 뭘 답한 건지 모호 → 한 번 더
    assert "4층이 없는 걸로 알고 있어요" in again["follow_up_question"]
    kept = send(client, sid, "4층이 맞아요").json()
    assert kept["confirm_required"] is True and kept["slots_filled"]["floor"] == "4"


def test_place_choice_corridor_replaces_room_question(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "강의실 와이파이가 안 터져요")
    send(client, sid, "혜인관 3층")
    body = send(client, sid, "복도").json()  # 강의실이 아니라 복도 — 호수를 더 안 물음
    assert body["confirm_required"] is True
    assert body["slots_filled"]["detail"] == "복도"


def test_bare_number_answer_is_understood_as_floor(client: TestClient) -> None:
    """"3"만 답해도 직전 질문(층)에 대한 답으로 3층으로 이해 — 같은 질문을 반복하면 안 됨."""
    sid = new_session(client)
    ask = begin(client, sid, "4층 에어컨이 안나와요")
    assert "어느 건물 4층인가요?" in ask["follow_up_question"]  # 층을 이미 알면 건물만 물음
    floor4 = send(client, sid, "북악관").json()
    assert "4층이 없는 걸로 알고 있어요" in floor4["follow_up_question"]
    nxt = send(client, sid, "3").json()
    assert nxt["slots_filled"]["floor"] == "3"
    assert "4층이 없는 걸로" not in nxt["follow_up_question"]
    assert "어느 호실인가요?" in nxt["follow_up_question"]
    summary = send(client, sid, "복도").json()
    assert "북악관 3층 복도" in summary["summary"] and "이해하지 못해서" not in summary["summary"]


def test_unresolved_fourth_floor_is_stated_honestly_in_summary(client: TestClient) -> None:
    """층 확인이 끝내 안 되면 4층으로 두되, 이해하지 못해서 그렇게 적었다고 요약에서 밝힘."""
    sid = new_session(client)
    begin(client, sid, "4층 에어컨이 안나와요")
    send(client, sid, "북악관")
    send(client, sid, "어")
    send(client, sid, "어")
    summary = send(client, sid, "복도").json()
    assert summary["confirm_required"] is True and summary["slots_filled"]["floor"] == "4"
    assert "층은 정확히 이해하지 못해서 처음 말씀하신 4층으로 적어 뒀어요." in summary["summary"]


def test_fourth_floor_affirmed_earlier_is_not_asked_again(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "4층 에어컨이 안나와요")
    send(client, sid, "북악관")
    send(client, sid, "4층이 맞아요")
    summary = send(client, sid, "복도").json()
    assert summary["confirm_required"] is True
    assert "4층이 없는 걸로" not in summary["summary"] and "이해하지 못해서" not in summary["summary"]


def test_room_number_fills_floor_without_asking(client: TestClient) -> None:
    """"301호"라고만 해도 3층으로 이해 — 층을 다시 묻지 않음."""
    sid = new_session(client)
    begin(client, sid, "강의실 와이파이가 안 터져요")
    room = send(client, sid, "혜인관").json()
    assert "몇 층" in room["follow_up_question"] or "몇 호" in room["follow_up_question"]
    summary = send(client, sid, "301호").json()
    assert summary["confirm_required"] is True
    assert summary["slots_filled"]["floor"] == "3"
    assert "혜인관 3층 301호 강의실" in summary["summary"]


def test_room_number_corrects_fourth_floor_in_building_without_one(client: TestClient) -> None:
    sid = new_session(client)
    begin(client, sid, "4층 에어컨이 안나와요")
    floor4 = send(client, sid, "북악관").json()
    assert "4층이 없는 걸로 알고 있어요" in floor4["follow_up_question"]
    summary = send(client, sid, "301호").json()  # 층 대신 호수로 답 → 3층
    assert summary["confirm_required"] is True and summary["slots_filled"]["floor"] == "3"
    assert "이해하지 못해서" not in summary["summary"]


def test_building_without_classrooms_is_not_reported_as_classroom(client: TestClient) -> None:
    """청운관엔 강의실이 없음 — "강의실"로 접수하지 않고 실제 층·호실 이름을 고르게 함."""
    sid = new_session(client)
    begin(client, sid, "강의실 와이파이가 안 터져요")
    floor = send(client, sid, "청운관").json()
    assert "4층" not in floor["choices"] and "3층" in floor["choices"]
    room = send(client, sid, "3층").json()
    assert "301호 학생과 사무실" in room["choices"]
    summary = send(client, sid, "301호 학생과 사무실").json()
    assert summary["confirm_required"] is True
    assert "청운관 3층 301호 학생과 사무실" in summary["summary"]
    assert "강의실 301" not in summary["summary"]


def test_named_facility_is_filed_without_asking_building(client: TestClient) -> None:
    sid = new_session(client)
    summary = begin(client, sid, "스포렉스 샤워실 온수가 안 나와요")
    assert summary["confirm_required"] is True
    assert summary["slots_filled"]["building"] == "유담관"


def test_vague_problem_is_asked_not_accepted(client: TestClient) -> None:
    """"4층이 이상해" — 무슨 문제인지 모르면 위치를 다 알아도 상황을 물어야 함 (실제 화면 대화)."""
    sid = new_session(client)
    begin(client, sid, "4층이 이상해")
    send(client, sid, "북악").json()
    send(client, sid, "5").json()
    ask = send(client, sid, "복도").json()
    assert ask.get("confirm_required") is not True
    assert "어떤 문제" in ask["follow_up_question"]
    summary = send(client, sid, "조명이 안 켜져요").json()
    assert summary["confirm_required"] is True
    assert summary["slots_filled"]["category"] == "전기"


def sse_text(resp) -> str:  # type: ignore[no-untyped-def]
    import json as _json

    out = ""
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            payload = _json.loads(line[6:])
            out += payload.get("delta", "")
    return out


def test_greeting_gets_short_reply_without_starting_a_flow(client: TestClient) -> None:
    sid = new_session(client)
    resp = send(client, sid, "안녕")
    assert resp.headers["content-type"].startswith("text/event-stream")
    text = sse_text(resp)
    assert "무엇에 대해 말씀하시는" not in text and len(text) < 120
    # 흐름이 시작되지 않았으므로 이어서 신고하면 처음부터 (제안 단계)
    offer = send(client, sid, "3층 정수기가 고장났어요").json()
    assert offer["choices"][0] == "네, 접수해 주세요"


def test_off_topic_is_blocked_and_repeats_differ(client: TestClient) -> None:
    sid = new_session(client)
    first = sse_text(send(client, sid, "오늘 날씨 알려줘"))
    second = sse_text(send(client, sid, "햄버거 만드는 법"))
    third = sse_text(send(client, sid, "파이썬 코드 짜줘"))
    assert first and second and third
    assert len({first, second, third}) == 3  # 같은 문장 반복 금지
    assert len(second) <= len(first) + 10 and "신고" in second  # 두 번째부터는 짧고 단호, 할 수 있는 일은 안내
