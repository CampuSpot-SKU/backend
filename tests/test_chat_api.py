"""챗봇 API 통합테스트 — 실제 PostgreSQL에서 세션 생성 → 대화 → 요약 확인 → 신고 접수까지 (작업 1-3, 1-3c).

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
from sqlalchemy import func, select

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.models import Building, Report, ReportStatusHistory
from app.models.enums import ReportStatus
from app.routers.chat import get_intent_classifier
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
    res = send(client, sid, "혜인관 2층 화장실 물이 계속 새요")
    assert res.status_code == 200
    body = res.json()
    # 슬롯이 다 차도 바로 접수하지 않고 요약 확인
    assert body["intent"] == "report" and body["confirm_required"] is True
    assert "report_created" not in body and report_count() == before
    assert body["summary"].startswith("신고 접수를 도와드릴게요. 이 내용으로 신고를 접수할까요?")
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
    assert "위치: 혜인관 2층 화장실" in done["message"] and "판정 이유" in done["message"]

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
    body = send(client, sid, "혜인관 2층 화장실 물이 계속 새요").json()
    assert body["confirm_required"] is True
    form = {"building": "혜인관", "floor": "7", "detail": "701호", "description": "화장실 물이 새요"}
    done = send(client, sid, "접수", action="confirm_report", draft=form).json()
    assert done["report_created"] is True
    report = get_report(done["report"]["display_no"])
    assert report.building_id == building_id("혜인관")  # 폼의 건물 이름 → 건물 목록과 매칭
    assert report.floor == "7" and report.detail == "701호"
    assert report.location_raw is None
    assert "위치: 혜인관 7층 701호" in done["message"]


def test_form_with_custom_building_text_is_kept_as_raw(client: TestClient) -> None:
    sid = new_session(client)
    send(client, sid, "혜인관 2층 화장실 물이 계속 새요")
    form = {"building": "체육관 옆 창고", "floor": "", "detail": "", "description": ""}
    done = send(client, sid, "접수", action="confirm_report", draft=form).json()
    assert done["report_created"] is True
    report = get_report(done["report"]["display_no"])
    assert report.building_id is None and report.location_raw == "체육관 옆 창고"


def test_slot_filling_asks_location_then_summary_then_creates(client: TestClient) -> None:
    sid = new_session(client)
    first = send(client, sid, "물이 계속 새요").json()
    assert first["intent"] == "report" and "confirm_required" not in first
    assert first["follow_up_question"].startswith("신고 접수를 도와드릴게요. 어디에서")
    assert first["slots_filled"]["location"] is None
    assert first["slots_filled"]["building"] is None  # 1-3c 형식: 필드는 항상 있음(값이 null)
    assert first["choices"] is None
    # 답변은 의도분류 없이 이어서 처리 (FAKE에 없는 문장이어도 됨)
    second = send(client, sid, "혜인관 2층 화장실이요").json()
    assert second["confirm_required"] is True
    assert not second["summary"].startswith("신고 접수를 도와드릴게요")  # 인사는 시작에만
    third = send(client, sid, "접수", action="confirm_report").json()
    assert third["report_created"] is True
    # 접수 후 다음 메시지는 새 대화
    fourth = send(client, sid, "휴학 신청 어떻게 해요?")
    assert fourth.headers["content-type"].startswith("text/event-stream")


def test_correction_in_summary_updates_and_summarizes_again(client: TestClient) -> None:
    sid = new_session(client)
    send(client, sid, "혜인관 2층 화장실 물이 계속 새요")
    again = send(client, sid, "아니 3층이에요").json()
    assert again["confirm_required"] is True
    assert again["slots_filled"]["floor"] == "3"  # 나중에 말한 층이 우선
    done = send(client, sid, "네").json()
    assert done["report_created"] is True
    assert get_report(done["report"]["display_no"]).floor == "3"


def test_cancel_during_slot_filling(client: TestClient) -> None:
    sid = new_session(client)
    send(client, sid, "물이 계속 새요")
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
    send(client, sid, "혜인관 2층 화장실 물이 계속 새요")
    res = send(client, sid, "취소", action="cancel_report").json()
    assert res["report_cancelled"] is True
    assert report_count() == before


def test_switch_to_inquiry_ends_report_flow(client: TestClient) -> None:
    sid = new_session(client)
    before = report_count()
    assert send(client, sid, "물이 계속 새요").json()["intent"] == "report"
    res = send(client, sid, "안내만 받을래요", action="switch_to_inquiry")
    assert res.headers["content-type"].startswith("text/event-stream")
    assert sse_text(res)
    # 신고 흐름이 끝났으므로 다음 신고 문장은 새로 시작 (이전 되묻기에 대한 답으로 안 먹힘)
    nxt = send(client, sid, "혜인관 2층 화장실 물이 계속 새요").json()
    assert nxt["confirm_required"] is True
    assert report_count() == before


def test_switch_to_inquiry_by_text_during_flow(client: TestClient) -> None:
    sid = new_session(client)
    send(client, sid, "물이 계속 새요")
    res = send(client, sid, "안내만 받을래요")
    assert res.headers["content-type"].startswith("text/event-stream")


# ── 상황(description)에 위치 답변·질문이 섞이지 않음 ────────────────────────────
def test_location_answer_is_not_added_to_description(client: TestClient) -> None:
    sid = new_session(client)
    send(client, sid, "강의실 와이파이가 안 터져요")
    second = send(client, sid, "혜인관 3층이요").json()
    assert second["confirm_required"] is True
    assert second["slots_filled"]["description"] == "강의실 와이파이가 안 터져요"
    assert "상황: 강의실 와이파이가 안 터져요\n" in second["summary"]  # "혜인관 3층이요"가 안 붙음
    done = send(client, sid, "접수").json()
    report = get_report(done["report"]["display_no"])
    assert report.description == "강의실 와이파이가 안 터져요"
    assert report.floor == "3"  # 위치는 답변에서 그대로 반영


def test_question_reply_to_location_ask_is_ignored(client: TestClient) -> None:
    sid = new_session(client)
    first = send(client, sid, "3층 정수기가 고장났어요").json()
    assert first["follow_up_question"].startswith("신고 접수를 도와드릴게요. 어디에서")
    second = send(client, sid, "3동이 우리학교에 있어?").json()
    # 질문은 위치로도 상황으로도 쓰지 않음 (없는 건물 "3동"이 채워지면 안 됨)
    assert second["confirm_required"] is True
    assert second["slots_filled"]["building"] is None
    assert second["slots_filled"]["floor"] == "3"
    assert second["slots_filled"]["description"] == "3층 정수기가 고장났어요"
    assert "우리학교" not in second["summary"]


def test_unknown_building_is_not_accepted_as_building(client: TestClient) -> None:
    sid = new_session(client)
    first = send(client, sid, "3동 2층 화장실 물이 계속 새요").json()
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
    send(client, sid, "공학관 301호 프로젝터가 안 켜져요")  # 공학관은 학교에 없음 → 되묻기
    second = send(client, sid, "모르겠어요").json()
    assert second["confirm_required"] is True  # 되묻기는 1번뿐
    assert second["slots_filled"]["building"] is None  # 건물로는 절대 안 채움
    assert "학교 건물 목록에 없는 이름" in second["summary"]


def test_location_answer_with_problem_text_is_kept_in_description(client: TestClient) -> None:
    sid = new_session(client)
    send(client, sid, "물이 계속 새요")
    second = send(client, sid, "혜인관 3층 화장실이요, 변기가 막혔어요").json()
    assert second["confirm_required"] is True
    assert "변기가 막혔어요" in second["slots_filled"]["description"]


# ── 접수 직후의 인사 (명세 4-1 보완 ②) ────────────────────────────────────────
def test_thanks_after_report_is_not_a_new_report(client: TestClient) -> None:
    sid = new_session(client)
    send(client, sid, "혜인관 2층 화장실 물이 계속 새요")
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
    first = send(client, sid, "은주관 3층 화장실 물이 새요").json()
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
    send(client, sid, "은주관 3층 화장실 물이 새요")
    second = send(client, sid, "2관").json()
    assert second["slots_filled"]["building"] == "은주2관"


def test_eunju_unsure_keeps_raw_location(client: TestClient) -> None:
    sid = new_session(client)
    send(client, sid, "은주관 3층 화장실 물이 새요")
    second = send(client, sid, "잘 모르겠어요").json()
    assert second["confirm_required"] is True  # 위치 되묻기는 1번뿐 — 더 묻지 않음
    done = send(client, sid, "접수").json()
    report = get_report(done["report"]["display_no"])
    assert report.building_id is None
    assert report.location_raw is not None and "은주관(1·2관 미확정)" in report.location_raw


def test_fourth_floor_in_building_without_one_asks_once(client: TestClient) -> None:
    sid = new_session(client)
    first = send(client, sid, "대일관 4층 복도 조명이 깜빡거려요").json()
    assert "4층 표기가 없어요" in first["follow_up_question"]
    second = send(client, sid, "아 3층이에요").json()
    assert second["confirm_required"] is True
    assert second["slots_filled"]["floor"] == "3"


def test_fourth_floor_confirmed_is_kept(client: TestClient) -> None:
    sid = new_session(client)
    send(client, sid, "대일관 4층 복도 조명이 깜빡거려요")
    second = send(client, sid, "네 4층 맞아요").json()
    assert second["confirm_required"] is True and second["slots_filled"]["floor"] == "4"


def test_generic_place_name_only_asks_for_building(client: TestClient) -> None:
    sid = new_session(client)
    first = send(client, sid, "강의실 와이파이가 안 터져요").json()
    assert "어느 건물 몇 층 강의실인가요?" in first["follow_up_question"]
    second = send(client, sid, "혜인관 3층이요").json()
    assert second["confirm_required"] is True
    assert second["slots_filled"]["building"] == "혜인관"


def test_floor_only_asks_location_once_then_summarizes(client: TestClient) -> None:
    sid = new_session(client)
    first = send(client, sid, "3층 정수기가 고장났어요").json()
    assert first["follow_up_question"].startswith("신고 접수를 도와드릴게요. 어디에서")
    second = send(client, sid, "모르겠어요").json()
    assert second["confirm_required"] is True  # 같은 질문은 1번만
    assert second["slots_filled"]["building"] is None


def test_facility_name_fills_building(client: TestClient) -> None:
    sid = new_session(client)
    body = send(client, sid, "스포렉스 샤워실 온수가 안 나와요").json()
    assert body["confirm_required"] is True  # 특정 시설 이름은 위치로 인정 — 건물을 안 묻고
    assert body["slots_filled"]["building"] == "유담관"
    assert body["slots_filled"]["floor"] == "3"
    assert "스포렉스" in body["slots_filled"]["detail"]
    done = send(client, sid, "접수").json()
    assert get_report(done["report"]["display_no"]).building_id == building_id("유담관")


def test_building_table_match_in_chat(client: TestClient) -> None:
    sid = new_session(client)
    body = send(client, sid, "혜인관 7층 화장실 물이 새요").json()
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
    res = send(client, sid, "신고해 주세요").json()
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


def test_locations_endpoint(client: TestClient) -> None:
    res = client.get("/api/v1/locations")
    assert res.status_code == 200
    buildings = res.json()["buildings"]
    names = [b["name"] for b in buildings if "name" in b]
    assert "은주1관" in names and "은주2관" in names
    assert buildings[-1]["custom"] is True  # 단계마다 맨 끝은 "목록에 없음 (직접 입력)"
    first_floor = buildings[0]["floors"][0]
    assert first_floor["places"][-1]["custom"] is True
