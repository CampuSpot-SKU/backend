"""슬롯 추출·대화 재구성·위치 처리 규칙 유닛테스트 — DB/네트워크 없이 실행 (작업 1-3, 1-3c)."""
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from app.models.enums import ChatIntent, ChatRole, Level
from app.services.slot_filling import (
    ASK_EUNJU,
    ASK_LOC,
    ASK_LOCATION,
    ASK_PROB,
    ASK_PROBLEM,
    CANCEL_HINT,
    EUNJU_CHOICES,
    START_GREETING,
    SUMMARY_PREFIX,
    BuildingRef,
    apply_form,
    build_summary,
    collect_draft,
    extract_slots,
    is_cancel,
    is_confirm,
    is_short_thanks,
    judge_reason,
    next_question,
    wants_inquiry,
)


@dataclass
class Msg:
    role: ChatRole
    content: str
    intent: ChatIntent | None = None
    intent_scores: dict[str, Any] | None = None


U, A = ChatRole.USER, ChatRole.ASSISTANT
REPORT, UNCLEAR = ChatIntent.REPORT, ChatIntent.UNCLEAR


# ── extract_slots ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "category", "location"),
    [
        ("3동 2층 화장실 물이 계속 새요", "시설·설비", "3동 2층 화장실"),
        ("복도 조명이 깜빡거려요", "전기", "복도"),
        ("정문 근처 벤치가 부서져 있어요", "시설·설비", "정문"),
        ("공학관 301호 프로젝터가 안 켜져요", "IT·네트워크", "공학관 301호"),
        ("도서관 열람실 와이파이가 안 터져요", "IT·네트워크", "도서관 열람실"),
        ("학생회관 지하1층 화장실 냄새가 너무 심해요", "청소·위생", "학생회관 지하 1층 화장실"),
        ("계단이 미끄러워요", "안전", "계단"),
    ],
)
def test_extract_category_and_location(text: str, category: str, location: str) -> None:
    slots = extract_slots(text)
    assert slots.category == category
    assert slots.location_text == location
    assert slots.has_problem


def test_building_table_match_wins_over_pattern() -> None:
    bid = uuid.uuid4()
    buildings = [BuildingRef(id=bid, name="제1공학관", aliases=("3동", "공대"))]
    slots = extract_slots("3동 2층 화장실 물이 새요", buildings)
    assert slots.building_id == bid
    assert slots.building == "제1공학관"
    assert slots.floor == "2"


def test_unmatched_building_keeps_text_without_id() -> None:
    slots = extract_slots("7동 엘리베이터 고장났어요", [])
    assert slots.building_id is None
    assert slots.building == "7동"


def test_detail_place_is_not_building() -> None:
    slots = extract_slots("현관 문이 안 닫혀요")
    assert slots.building is None
    assert slots.detail == "현관"


def test_no_false_building_from_common_words() -> None:
    # "상관", "작동" 같은 일반 단어를 건물로 오인하지 않음
    assert extract_slots("상관없이 작동이 안 돼요").has_location is False


@pytest.mark.parametrize(
    ("text", "safety", "impact", "urgency"),
    [
        ("3동 2층 화장실 물이 새요", False, Level.HIGH, Level.LOW),  # 공용공간, 안 급함 → P2
        ("계단이 미끄러워서 넘어질 뻔했어요", False, Level.HIGH, Level.HIGH),  # 안전 → P1
        ("연구실 콘센트에서 스파크가 튀어요", False, Level.LOW, Level.HIGH),  # 개인공간+위험 → P3
        ("연구실 의자가 부서졌어요", False, Level.LOW, Level.LOW),  # → P4
        ("3동 엘리베이터 고장났어요", True, Level.HIGH, Level.HIGH),  # ai safety_concern 반영
    ],
)
def test_impact_and_urgency(text: str, safety: bool, impact: Level, urgency: Level) -> None:
    slots = extract_slots(text, safety_concern=safety)
    assert (slots.impact, slots.urgency) == (impact, urgency)


def test_missing_slots() -> None:
    no_location = extract_slots("물이 계속 새요")
    assert not no_location.has_location and no_location.has_problem
    no_problem = extract_slots("3동 2층 화장실이요")
    assert no_problem.has_location and not no_problem.has_problem


# ── 위치 처리 규칙 (명세 4-1, 1-3c) ───────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "has_location"),
    [
        ("3층 정수기가 고장났어요", False),  # 층만 → 위치 아님 (한비 제안 ①)
        ("강의실 와이파이가 안 터져요", False),  # 일반 장소 이름만 (한비 제안)
        ("복도 조명이 깜빡거려요", False),
        ("화장실 물이 새요", False),
        ("301호 프로젝터가 안 켜져요", True),  # 호수는 위치로 인정
        ("스포렉스 샤워실 온수가 안 나와요", True),  # 특정 시설은 위치로 인정
        ("혜인관 화장실 물이 새요", True),
        ("정문 근처 벤치가 부서졌어요", True),
    ],
)
def test_has_location_rules(text: str, has_location: bool) -> None:
    assert extract_slots(text).has_location is has_location


def test_eunju_unresolved_is_ambiguous_and_not_added_as_alias() -> None:
    slots = extract_slots("은주관 3층 화장실 물이 새요")
    assert slots.ambiguous_building == "은주관(1·2관 미확정)"
    assert slots.building_id is None
    assert slots.location_text == "은주관(1·2관 미확정) 3층 화장실"
    q = next_question(slots, set())
    assert q is not None and q.text == ASK_EUNJU and q.choices == EUNJU_CHOICES


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("은주1관 3층 화장실", "은주1관"),
        ("은주 2관 3층 화장실", "은주2관"),
        ("은주관 3층 화장실 물이 새요\n1관이요", "은주1관"),  # 되묻기에 대한 답
        ("은주관 3층 화장실 물이 새요\n2관", "은주2관"),
    ],
)
def test_eunju_resolved(text: str, expected: str) -> None:
    ids = {"은주1관": uuid.uuid4(), "은주2관": uuid.uuid4()}
    buildings = [BuildingRef(id=i, name=n) for n, i in ids.items()]
    slots = extract_slots(text, buildings)
    assert slots.building == expected and slots.building_id == ids[expected]
    assert slots.ambiguous_building is None


def test_eunju_unsure_answer_stays_unresolved() -> None:
    slots = extract_slots("은주관 3층 화장실 물이 새요\n잘 모르겠어요")
    assert slots.ambiguous_building is not None and slots.building_id is None


def test_fourth_floor_in_building_without_it() -> None:
    assert extract_slots("대일관 4층 복도 조명이 깜빡거려요").floor_check
    assert extract_slots("문예관 4층 화장실 물이 새요").floor_check
    assert not extract_slots("혜인관 4층 화장실 물이 새요").floor_check  # 4층이 있는 건물
    assert not extract_slots("대일관 3층 화장실 물이 새요").floor_check
    q = next_question(extract_slots("대일관 4층 복도 조명이 깜빡거려요"), set())
    assert q is not None and "대일관에는 4층 표기가 없어요" in q.text


def test_later_floor_wins_on_correction() -> None:
    assert extract_slots("대일관 4층 복도 조명이 깜빡여요\n3층이요").floor == "3"


def test_facility_name_fills_building_and_floor() -> None:
    ids = {"유담관": uuid.uuid4()}
    slots = extract_slots("스포렉스 샤워실 온수가 안 나와요", [BuildingRef(ids["유담관"], "유담관")])
    assert slots.building == "유담관" and slots.building_id == ids["유담관"]
    assert slots.floor == "3"
    assert slots.detail == "서경스포렉스 샤워실" and slots.detail_specific


def test_toilet_gender_only_when_student_says_it() -> None:
    assert extract_slots("혜인관 3층 남자 화장실 물이 새요").detail == "남자 화장실"
    assert extract_slots("혜인관 3층 여자화장실 물이 새요").detail == "여자 화장실"
    assert extract_slots("혜인관 3층 화장실 물이 새요").detail == "화장실"  # 추측 금지


def test_basement_floor() -> None:
    slots = extract_slots("북악관 B1 화장실 물이 새요")
    assert slots.floor == "B1" and slots.location_text == "북악관 지하 1층 화장실"


def test_apply_form_overrides_location_and_recomputes_impact() -> None:
    bid = uuid.uuid4()
    buildings = [BuildingRef(bid, "혜인관")]
    slots = extract_slots("3동 2층 화장실 물이 새요", buildings)
    out = apply_form(slots, "혜인관", "7", "701호", buildings, "3동 2층 화장실 물이 새요")
    assert out.building_id == bid and out.building == "혜인관"
    assert out.floor == "7" and out.detail == "701호" and out.detail_specific
    # 폼의 빈 값 = 학생이 비움
    blank = apply_form(slots, "", "", "", buildings, "물이 새요")
    assert blank.building is None and blank.floor is None and blank.detail is None
    assert blank.impact == Level.LOW  # 위치가 없으면 영향도 "저"
    # None이면 그대로
    assert apply_form(slots, None, None, None, buildings, "x").building == "3동"
    # 층 표기 정리
    assert apply_form(slots, None, "지하1층", None, buildings, "x").floor == "B1"
    assert apply_form(slots, None, "B2", None, buildings, "x").floor == "B2"
    # 목록에 없는 건물은 이름 그대로 (id 없음 → location_raw)
    custom = apply_form(slots, "체육관 옆 창고", None, None, buildings, "x")
    assert custom.building_id is None and custom.building == "체육관 옆 창고"


# ── next_question ───────────────────────────────────────────────────────────
def test_next_question_order_and_asks_only_once() -> None:
    empty = extract_slots("저기요")
    q1 = next_question(empty, set())
    assert q1 is not None and q1.text == ASK_LOCATION and q1.key == ASK_LOC
    q2 = next_question(empty, {ASK_LOC})
    assert q2 is not None and q2.text == ASK_PROBLEM and q2.key == ASK_PROB
    assert next_question(empty, {ASK_LOC, ASK_PROB}) is None  # 더 안 묻고 요약으로
    assert next_question(extract_slots("3동 화장실 물이 새요"), set()) is None


def test_location_question_kinds_share_one_ask() -> None:
    # 은주관을 되묻고 난 뒤엔 4층 확인·일반 장소 확인 등 다른 위치 질문을 또 하지 않음
    slots = extract_slots("은주관 3층 화장실 물이 새요")
    assert next_question(slots, {ASK_LOC}) is None
    slots = extract_slots("강의실 와이파이가 안 터져요")
    q = next_question(slots, set())
    assert q is not None and q.text == "어느 건물 몇 층 강의실인가요? (예: 은주1관 3층 강의실)"
    assert next_question(slots, {ASK_LOC}) is None


# ── collect_draft ───────────────────────────────────────────────────────────
def test_draft_empty_for_new_session() -> None:
    draft = collect_draft([])
    assert draft.user_texts == [] and not draft.in_progress


def test_draft_in_progress_after_follow_up() -> None:
    history = [
        Msg(U, "휴학 어떻게 해요?", ChatIntent.INQUIRY),
        Msg(A, "준비 중이에요", ChatIntent.INQUIRY),
        Msg(U, "물이 계속 새요", REPORT, {"safety_concern": True}),
        Msg(A, ASK_LOCATION + CANCEL_HINT, REPORT),
    ]
    draft = collect_draft(history)
    assert draft.in_progress
    assert draft.user_texts == ["물이 계속 새요"]  # 앞의 행정문의는 안 섞임
    assert draft.asked == {ASK_LOC}
    assert draft.safety_concern


def test_draft_ends_after_report_created() -> None:
    history = [
        Msg(U, "3동 화장실 물이 새요", REPORT),
        Msg(A, "신고가 접수됐어요! 접수번호는 1번이에요.", None),
    ]
    draft = collect_draft(history)
    assert not draft.in_progress and draft.user_texts == []


def test_draft_includes_sentence_before_clarifying_question() -> None:
    history = [
        Msg(U, "계단이 미끄러운데 어떻게 해야 하나요?", UNCLEAR),
        Msg(A, "이걸 신고로 접수해드릴까요, 안내가 필요하신 건가요?", UNCLEAR),
    ]
    draft = collect_draft(history)
    assert not draft.in_progress
    assert draft.user_texts == ["계단이 미끄러운데 어떻게 해야 하나요?"]


def test_is_cancel() -> None:
    assert is_cancel("취소할게요")
    assert not is_cancel("2층이요")


def test_draft_recognizes_questions_after_greeting() -> None:
    history = [
        Msg(U, "은주관 화장실 물이 새요", REPORT),
        Msg(A, START_GREETING + ASK_EUNJU + CANCEL_HINT, REPORT),
    ]
    draft = collect_draft(history)
    assert draft.in_progress and not draft.confirming
    assert draft.asked == {ASK_LOC}


def test_draft_confirming_after_summary() -> None:
    slots = extract_slots("3동 2층 화장실 물이 새요")
    summary = build_summary(slots, ["3동 2층 화장실 물이 새요"], greeting=True)
    history = [Msg(U, "3동 2층 화장실 물이 새요", REPORT), Msg(A, summary, REPORT)]
    draft = collect_draft(history)
    assert draft.in_progress and draft.confirming
    assert draft.user_texts == ["3동 2층 화장실 물이 새요"]


def test_summary_text() -> None:
    slots = extract_slots("3동 2층 화장실 물이 계속 새요")
    text = build_summary(slots, ["3동 2층 화장실 물이 계속 새요"], greeting=True)
    assert text.startswith(START_GREETING + SUMMARY_PREFIX)
    assert "· 위치: 3동 2층 화장실" in text and "· 상황: 3동 2층 화장실 물이 계속 새요" in text
    assert "[접수]" in text and "[취소]" in text
    assert not build_summary(slots, ["x"], greeting=False).startswith(START_GREETING)
    # 위치를 끝내 모르면 그대로 알림 (관리자가 확인)
    empty = build_summary(extract_slots("물이 새요"), ["물이 새요"], greeting=False)
    assert "확인되지 않았어요" in empty


@pytest.mark.parametrize("text", ["접수", "네", "[접수]", "응!", "네 접수해 주세요", "좋아요", "접수할게요"])
def test_is_confirm_true(text: str) -> None:
    assert is_confirm(text)


@pytest.mark.parametrize("text", ["2층이요", "접수 안 할래요", "아니요", "접수 말고 취소", "혜인관이요"])
def test_is_confirm_false(text: str) -> None:
    assert not is_confirm(text)


@pytest.mark.parametrize("text", ["고마워요", "감사합니다", "네", "넵 알겠습니다", "확인했어요"])
def test_is_short_thanks_true(text: str) -> None:
    assert is_short_thanks(text)


@pytest.mark.parametrize("text", ["3동 화장실 물이 새요", "휴학 어떻게 해요?", "물이 또 새요", "감사한데 다른 문제도 있어요 계단이 미끄러워요"])
def test_is_short_thanks_false(text: str) -> None:
    assert not is_short_thanks(text)


def test_wants_inquiry() -> None:
    assert wants_inquiry("안내만 받을래요")
    assert not wants_inquiry("2층이요")


def test_judge_reason() -> None:
    p1 = judge_reason(extract_slots("계단이 미끄러워요"), "P1")
    assert "여러 사람이 쓰는 공간" in p1 and "안전 위험 신호" in p1 and "P1" in p1
    p4 = judge_reason(extract_slots("연구실 의자가 부서졌어요"), "P4")
    assert "개인 공간" in p4 and "급한 위험 신호는 없어" in p4 and "P4" in p4
