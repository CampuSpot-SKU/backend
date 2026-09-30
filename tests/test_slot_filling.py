"""슬롯 추출·대화 재구성·위치 처리 규칙 유닛테스트 — DB/네트워크 없이 실행 (작업 1-3, 1-3c)."""
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from app.models.enums import ChatIntent, ChatRole, Level
from app.services.slot_filling import (
    ASK_AMB,
    ASK_EUNJU,
    ASK_FAC,
    ASK_LOC,
    ASK_LOCATION,
    ASK_NST,
    ASK_PROB,
    ASK_PROBLEM,
    EUNJU_CHOICES,
    KIND_CHOOSE_B,
    KIND_CHOOSE_F,
    KIND_FACILITY,
    KIND_NESTED,
    KIND_SPLIT,
    OFFER_MARKER,
    SUMMARY_MARKER,
    BuildingRef,
    apply_form,
    apply_judgement,
    build_offer,
    build_summary,
    collect_draft,
    extract_slots,
    find_ambiguity,
    is_cancel,
    is_confirm,
    is_edit,
    is_no,
    is_short_thanks,
    is_yes,
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
    debug_payload: dict[str, Any] | None = None


U, A = ChatRole.USER, ChatRole.ASSISTANT
REPORT, UNCLEAR = ChatIntent.REPORT, ChatIntent.UNCLEAR


# ── extract_slots ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "category", "location"),
    [
        ("혜인관 2층 화장실 물이 계속 새요", "시설·설비", "혜인관 2층 화장실"),
        ("3동 2층 화장실 물이 계속 새요", "시설·설비", "3동 2층 화장실"),  # 없는 건물도 글자는 남김(되묻기)
        ("복도 조명이 깜빡거려요", "전기", "복도"),
        ("정문 근처 벤치가 부서져 있어요", "시설·설비", "정문"),
        ("혜인관 301호 프로젝터가 안 켜져요", "IT·네트워크", "혜인관 3층 301호 강의실"),
        ("혜인관 열람실 와이파이가 안 터져요", "IT·네트워크", "혜인관 열람실"),
        ("청운관 지하1층 화장실 냄새가 너무 심해요", "청소·위생", "청운관 지하 1층 화장실"),
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


def test_unknown_building_name_is_not_a_building() -> None:
    # 학교에 없는 건물 이름("7동", "공학관")은 건물로 받지 않고 되물음
    for text, name in [("7동 엘리베이터 고장났어요", "7동"), ("공학관 301호 프로젝터가 안 켜져요", "공학관"),
                       ("도서관 열람실 와이파이가 안 터져요", "도서관")]:
        slots = extract_slots(text, [])
        assert slots.building is None and slots.building_id is None
        assert slots.unknown_place == name
        assert not slots.has_location  # 호수·열람실이 있어도 어느 건물인지 모름
        q = next_question(slots, set())
        assert q is not None and f"'{name}'은(는) 제가 아는 학교 장소에 없어요" in q.text
    # 한 번 물었는데도 모르면 다시 묻고, "모르겠어요"라고 하면(unsure) 그대로 요약으로
    again = next_question(extract_slots("7동 엘리베이터 고장났어요"), {ASK_LOC})
    assert again is not None and again.key == ASK_LOC
    assert next_question(extract_slots("7동 엘리베이터 고장났어요"), {ASK_LOC}, unsure=True) is None


def test_official_buildings_are_recognized_even_without_table() -> None:
    slots = extract_slots("혜인관 7층 화장실 물이 새요", [])
    assert slots.building == "혜인관" and slots.building_id is None and slots.has_location
    assert extract_slots("체육관 샤워실 물이 안 나와요").building == "수인관"  # 시설 이름 → 건물
    assert extract_slots("정문 근처 벤치가 부서졌어요").has_location  # 건물 밖 장소


def test_unknown_words_ending_in_gwan_are_not_buildings_but_places_are() -> None:
    assert extract_slots("체육관 조명이 나갔어요").unknown_place is None
    assert extract_slots("현관 문이 안 닫혀요").unknown_place is None


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
    no_problem = extract_slots("혜인관 2층 화장실이요")
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
    assert q is not None and "대일관에는 4층이 없는 걸로 알고 있어요" in q.text


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
    slots = extract_slots("청운관 2층 화장실 물이 새요", buildings)
    out = apply_form(slots, "혜인관", "7", "701호", buildings, "청운관 2층 화장실 물이 새요")
    assert out.building_id == bid and out.building == "혜인관"
    assert out.floor == "7" and out.detail == "701호" and out.detail_specific
    # 폼의 빈 값 = 학생이 비움
    blank = apply_form(slots, "", "", "", buildings, "물이 새요")
    assert blank.building is None and blank.floor is None and blank.detail is None
    assert blank.impact == Level.LOW  # 위치가 없으면 영향도 "저"
    # None이면 그대로
    assert apply_form(slots, None, None, None, buildings, "x").building == "청운관"
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
    q_again = next_question(empty, {ASK_LOC})  # 못 알아들었으면 건물을 다시 물음
    assert q_again is not None and q_again.key == ASK_LOC
    q2 = next_question(empty, {ASK_LOC}, unsure=True)  # "모르겠어요"면 다음 항목으로
    assert q2 is not None and q2.text == ASK_PROBLEM and q2.key == ASK_PROB
    assert next_question(empty, {ASK_LOC, ASK_PROB}, unsure=True) is None  # 더 안 묻고 요약으로
    # 그래도 무한히 묻지는 않음 (위치 질문 최대 3번)
    assert next_question(empty, {ASK_LOC, "location#2", "location#3", ASK_PROB}) is None
    assert next_question(extract_slots("혜인관 2층 화장실 물이 새요"), set()) is None
    # 건물까지만 알면 층·호수를 한 번 더 물음 (이미 물었으면 더 안 물음)
    q = next_question(extract_slots("혜인관 화장실 물이 새요"), set())
    assert q is not None and q.key == "floor" and "혜인관의 몇 층, 몇 호인가요?" in q.text
    assert next_question(extract_slots("혜인관 화장실 물이 새요"), {"floor"}, unsure=True) is None
    assert next_question(extract_slots("혜인관 화장실 물이 새요"), {"floor", "floor#2"}) is None
    assert next_question(extract_slots("혜인관 301호 프로젝터가 안 켜져요"), set()) is None  # 호수가 있음


def test_location_question_kinds_share_one_ask() -> None:
    # 은주관을 되묻고 난 뒤엔 4층 확인·일반 장소 확인 등 다른 위치 질문을 또 하지 않음
    slots = extract_slots("은주관 3층 화장실 물이 새요")
    assert next_question(slots, {ASK_LOC}, unsure=True) is None
    slots = extract_slots("강의실 와이파이가 안 터져요")
    q = next_question(slots, set())
    assert q is not None and q.text == "어느 건물 몇 층 강의실인가요? (예: 은주1관 3층 강의실)"
    again = next_question(slots, {ASK_LOC})  # 건물을 못 알아들었으면 알아낸 장소를 짚어 다시 물음
    assert again is not None and again.text == "어느 건물의 강의실인가요?"
    assert next_question(slots, {ASK_LOC}, unsure=True) is None


# ── collect_draft ───────────────────────────────────────────────────────────
def test_draft_empty_for_new_session() -> None:
    draft = collect_draft([])
    assert draft.user_texts == [] and not draft.in_progress


def test_draft_in_progress_after_follow_up() -> None:
    history = [
        Msg(U, "휴학 어떻게 해요?", ChatIntent.INQUIRY),
        Msg(A, "준비 중이에요", ChatIntent.INQUIRY),
        Msg(U, "물이 계속 새요", REPORT, {"safety_concern": True}),
        Msg(A, ASK_LOCATION, REPORT),
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
        Msg(A, ASK_EUNJU, REPORT),
    ]
    draft = collect_draft(history)
    assert draft.in_progress and not draft.confirming
    assert draft.asked == {ASK_LOC}


def test_draft_confirming_after_summary() -> None:
    slots = extract_slots("3동 2층 화장실 물이 새요")
    summary = build_summary(slots, ["3동 2층 화장실 물이 새요"])
    history = [Msg(U, "3동 2층 화장실 물이 새요", REPORT), Msg(A, summary, REPORT)]
    draft = collect_draft(history)
    assert draft.in_progress and draft.confirming
    assert draft.user_texts == ["3동 2층 화장실 물이 새요"]


def test_summary_is_one_sentence_not_a_list() -> None:
    slots = extract_slots("혜인관 2층 화장실 물이 계속 새요")
    text = build_summary(slots, ["혜인관 2층 화장실 물이 계속 새요"])
    assert SUMMARY_MARKER in text
    assert "혜인관 2층 화장실에서" in text and "물이 계속 새요" in text
    assert "위치:" not in text and "상황:" not in text and "·" not in text and "\n" not in text
    # 위치를 끝내 모르면 담당자가 확인한다고 말함
    assert "담당자가 확인" in build_summary(extract_slots("물이 새요"), ["물이 새요"])


def test_offer_text() -> None:
    text = build_offer("3층 정수기가 고장났어요")
    assert OFFER_MARKER in text and "3층 정수기가 고장났어요" in text


def test_draft_kind_from_payload_survives_rephrasing() -> None:
    """Gemini가 말투를 바꿔도(고정 문구 표식이 없어도) 저장된 kind로 단계를 알아봄."""
    history = [
        Msg(U, "3층 정수기가 고장났어요", REPORT),
        Msg(A, "정수기 문제시군요! 접수 도와드릴까요?", REPORT, debug_payload={"kind": "offer"}),
    ]
    draft = collect_draft(history)
    assert draft.in_progress and draft.last_kind == "offer" and not draft.confirming
    history += [
        Msg(U, "응", REPORT),
        Msg(A, "어느 건물이에요?", REPORT, debug_payload={"kind": "location"}),
    ]
    assert collect_draft(history).asked == {ASK_LOC}
    history += [
        Msg(U, "혜인관이요", REPORT),
        Msg(A, "이렇게 접수하면 될까요?", REPORT, debug_payload={"kind": "summary"}),
    ]
    assert collect_draft(history).confirming


def test_offer_reply_question_is_ignored() -> None:
    draft = collect_draft([
        Msg(U, "물이 새요", REPORT),
        Msg(A, build_offer("물이 새요"), REPORT),
    ])
    assert draft.last_kind == "offer"
    assert draft.with_reply("정수기가 뭐예요?") == (["물이 새요"], ["물이 새요"])
    assert draft.with_reply("혜인관 3층이에요")[0][-1] == "혜인관 3층이에요"


@pytest.mark.parametrize("text", ["응", "네", "네, 접수해 주세요", "도와주세요", "그렇게 해줘", "ㅇㅇ"])
def test_is_yes_true(text: str) -> None:
    assert is_yes(text)


@pytest.mark.parametrize("text", ["아니요", "아니요, 안내만 받을게요", "괜찮아요", "혜인관 3층이에요"])
def test_is_yes_false(text: str) -> None:
    assert not is_yes(text)


def test_is_no_and_edit() -> None:
    assert is_no("아니요") and not is_no("응")
    assert is_edit("내용을 고칠래요") and is_edit("아니요") and is_edit("수정")
    assert not is_edit("아니 4층이에요 혜인관 4층")  # 고칠 내용이 같이 오면 정정으로 처리


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


def test_draft_description_excludes_location_answers_and_questions() -> None:
    history = [
        Msg(U, "강의실 와이파이가 안 터져요", REPORT),
        Msg(A, "어느 건물 몇 층 강의실인가요? (예: 은주1관 3층 강의실)", REPORT),
        Msg(U, "3동이 우리학교에 있어?", REPORT),
        Msg(A, "어느 건물 몇 층 강의실인가요?", REPORT),
        Msg(U, "혜인관 3층이요", REPORT),
    ]
    # 위 구간은 마지막이 사용자 메시지라 진행 중은 아니지만 재구성 규칙은 같음
    draft = collect_draft(history)
    assert draft.user_texts == ["강의실 와이파이가 안 터져요", "혜인관 3층이요"]  # 질문은 버림
    assert draft.desc_texts == ["강의실 와이파이가 안 터져요"]  # 위치 답변은 상황에서 뺌


def test_draft_with_reply_follows_last_question_kind() -> None:
    history = [
        Msg(U, "물이 새요", REPORT),
        Msg(A, ASK_LOCATION, REPORT),
    ]
    draft = collect_draft(history)
    assert draft.last_kind == "location"
    assert draft.with_reply("3동 2층 화장실이요") == (["물이 새요", "3동 2층 화장실이요"], ["물이 새요"])
    assert draft.with_reply("여기 어디예요?") == (["물이 새요"], ["물이 새요"])
    # 상황을 되물은 뒤의 답은 상황으로 씀
    history = [Msg(U, "3동 2층 화장실이요", REPORT), Msg(A, ASK_PROBLEM, REPORT)]
    assert collect_draft(history).with_reply("물이 새요")[1] == ["3동 2층 화장실이요", "물이 새요"]


def test_building_abbreviation_is_recognized() -> None:
    assert extract_slots("북악 4층").building == "북악관"
    assert extract_slots("혜인 3층 화장실").building == "혜인관"
    assert extract_slots("은주 3층").building != "은주1관"  # 은주는 1·2관이 갈려 약칭으로 받지 않음
    assert extract_slots("본 건물 3층").building is None


def test_detail_uses_latest_place_and_room() -> None:
    assert extract_slots("강의실 와이파이가 안 터져요\n혜인관 3층 복도").detail == "복도"
    assert extract_slots("강의실 와이파이가 안 터져요\n301호").detail == "301호 강의실"
    assert extract_slots("301호 강의실 프로젝터\n아니 302호요").detail == "302호 강의실"


def test_room_and_place_questions() -> None:
    slots = extract_slots("혜인관 3층 강의실 와이파이가 안 터져요")
    q = next_question(slots, set())
    assert q is not None and q.key == "place" and "몇 호 강의실인가요?" in q.text
    assert q.choices is not None and "복도" in q.choices and q.choices[-1] == "잘 모르겠어요"
    assert "301호 강의실" in q.choices  # 그 층에 실제 있는 강의실
    # 호수가 있거나 방이 아닌 장소(복도·화장실)면 더 안 물음
    assert next_question(extract_slots("혜인관 3층 301호 강의실 와이파이가 안 터져요"), set()) is None
    assert next_question(extract_slots("혜인관 3층 복도 조명이 깜빡거려요"), set()) is None
    # 장소를 전혀 모르면 호수·장소를 고르게 물음
    none_q = next_question(extract_slots("혜인관 3층 와이파이가 안 터져요"), set())
    assert none_q is not None and none_q.key == "place" and "어느 호실인가요?" in none_q.text
    # 모른다고 하면 중단, 최대 2번
    assert next_question(slots, {"place"}, unsure=True) is None
    assert next_question(slots, {"place", "place#2"}) is None


def test_bare_number_reply_follows_previous_question() -> None:
    def history(kind_text: str) -> list[Msg]:
        return [Msg(U, "4층 에어컨이 안나와요", REPORT), Msg(A, kind_text, REPORT, debug_payload={"kind": kind_text})]

    assert collect_draft(history("floor4")).with_reply("3")[0][-1] == "3층"
    assert collect_draft(history("location")).with_reply("2")[0][-1] == "2층"
    assert collect_draft(history("floor")).with_reply("3층이요")[0][-1] == "3층"
    assert collect_draft(history("place")).with_reply("301")[0][-1] == "301호"
    assert collect_draft(history("place")).with_reply("3")[0][-1] == "3"  # 장소 질문에 한 자리 숫자는 호수가 아님
    assert collect_draft(history("problem")).with_reply("3")[0][-1] == "3"  # 다른 질문엔 손대지 않음


def test_summary_admits_unresolved_fourth_floor() -> None:
    slots = extract_slots("대일관 4층 복도 조명이 깜빡거려요")
    assert "이해하지 못해서 처음 말씀하신 4층" in build_summary(slots, ["x"])
    assert "이해하지 못해서" not in build_summary(slots, ["x"], affirmed=True)


def test_room_number_implies_floor() -> None:
    assert extract_slots("혜인관 301호 프로젝터가 안 켜져요").floor == "3"
    assert extract_slots("혜인관 1201호 프로젝터가 안 켜져요").floor == "12"
    assert extract_slots("혜인관 2층 301호 프로젝터가 안 켜져요").floor == "2"  # 층을 직접 말했으면 그대로
    # 4층 없는 건물에서 4층이라고 했는데 호수가 다른 층이면 호수 기준 (확인 질문도 필요 없음)
    slots = extract_slots("북악관 4층 에어컨이 안 나와요\n301호")
    assert slots.floor == "3" and not slots.floor_check
    # 호수가 4xx면 4층 그대로 (건물에 4층이 없으면 확인)
    assert extract_slots("북악관 401호 에어컨이 안 나와요").floor_check
    assert next_question(extract_slots("혜인관 301호 프로젝터가 안 켜져요"), set()) is None  # 층을 또 묻지 않음


# ── 학교 데이터 대조 (app/data/location_options.json) ───────────────────────────
def test_named_facility_needs_no_building_or_floor() -> None:
    """이름만으로 위치가 정해지는 곳(스포렉스, 학교에 하나뿐인 공연장)은 건물·층을 묻지 않음."""
    for text, building in [("스포렉스 샤워실 온수가 안 나와요", "유담관"), ("공연장 조명이 나갔어요", "대일관")]:
        slots = extract_slots(text)
        assert slots.building == building and slots.floor and slots.detail_specific
        assert next_question(slots, set()) is None


def test_building_without_classrooms_does_not_take_classroom() -> None:
    slots = extract_slots("청운관 강의실 와이파이가 안 터져요")
    assert slots.building == "청운관" and slots.detail is None  # 없는 방을 만들지 않음
    q = next_question(slots, set())
    assert q is not None and q.key == "floor" and q.choices is not None
    assert "4층" not in q.choices and "3층" in q.choices and "5층" in q.choices  # 실제 있는 층만
    room_q = next_question(extract_slots("청운관 3층 강의실 와이파이가 안 터져요"), set())
    assert room_q is not None and room_q.key == "place"
    assert room_q.choices is not None and "301호 학생과 사무실" in room_q.choices  # 실제 호실 이름


def test_room_name_answer_resolves_to_real_room() -> None:
    slots = extract_slots("청운관 3층 와이파이가 안 터져요\n학생과")
    assert slots.detail == "301호 학생과 사무실" and slots.detail_specific


def test_floor_not_in_building_uses_real_floors() -> None:
    slots = extract_slots("청운관 4층 복도 불이 깜빡여요")
    assert slots.floor_check
    q = next_question(slots, set())
    assert q is not None and q.key == "floor4" and "지하 1층, 1~3층, 5~11층" in q.text
    assert next_question(slots, set(), affirmed=True) is None  # "4층이 맞아요"면 그대로


def test_unlisted_room_asks_once_then_is_marked() -> None:
    slots = extract_slots("혜인관 3층 399호 에어컨이 안 나와요")
    assert slots.room_unlisted and slots.detail == "399호"
    q = next_question(slots, set())
    assert q is not None and q.key == "roomcheck" and "399호가 맞아요" in (q.choices or [])
    assert next_question(slots, {"roomcheck"}) is None  # 한 번만
    assert "목록 확인" not in build_summary(slots, ["에어컨이 안 나와요"])
    assert "담당자가 다시 확인" in build_summary(slots, ["에어컨이 안 나와요"])
    assert "(목록에 없음)" in (slots.location_text or "")


def test_basement_room_number() -> None:
    slots = extract_slots("대일관 B101호 프로젝터가 안 켜져요")
    assert slots.floor == "B1"


def test_vague_complaint_is_not_a_known_problem() -> None:
    """"이상해요"만으로는 무슨 문제인지 모름 → 상황을 되묻는다."""
    slots = extract_slots("북악관 3층 복도가 이상해")
    assert not slots.has_problem
    q = next_question(slots, set())
    assert q is not None and q.key == "problem"
    assert extract_slots("북악관 3층 복도 불이 안 켜져요").has_problem


def test_unlisted_outdoor_place_is_asked_not_accepted() -> None:
    # "하늘공원"처럼 학교 데이터에 없는 야외 장소 이름은 있는 곳처럼 받지 않고 건물을 되물음
    for text in ["서경대 하늘공원에 불이 난 거 같아", "하늘공원 벤치가 부서졌어요"]:
        slots = extract_slots(text)
        assert slots.unknown_place == "하늘공원"
        assert not slots.has_location
    # 건물이 함께 있거나 학교에 있는 장소(정문)면 그대로 접수
    assert extract_slots("북악관 앞 정원 벤치가 부서졌어요").has_location
    assert extract_slots("정문 앞 광장 조명이 나갔어요").has_location


def test_fire_expressions_are_a_problem_and_urgent() -> None:
    # "불이 난 거 같아" — 상황을 아는 문장(안전·긴급)이어야 하고, "불이 나갔어요"(조명 꺼짐)와는 구분
    for text in ["하늘공원에 불이 난 거 같아", "북악관 3층에 불났어요", "혜인관 앞에서 불길이 보여요"]:
        slots = extract_slots(text)
        assert slots.has_problem and slots.category == "안전" and slots.urgency == Level.HIGH
    lights = extract_slots("북악관 3층 복도 불이 나갔어요")
    assert lights.category != "안전" and lights.urgency == Level.LOW


def test_outdoor_spot_near_building_needs_no_floor() -> None:
    # 건물 바깥(앞·옆·정원 등)은 층·호실이 없으니 층을 되묻지 않고 위치로 인정
    for text, where in [("북악관 앞 정원 벤치가 부서졌어요", "북악관 앞 정원"), ("혜인관 옆에서 넘어졌어요", "혜인관 옆")]:
        slots = extract_slots(text)
        assert slots.location_text == where and slots.detail_specific
        question = next_question(slots, set())
        assert question is None or question.key != "floor"
    # 층·호수·일반 장소를 말하면 기존처럼 층/장소를 따짐
    assert not extract_slots("북악관 벤치가 부서졌어요").detail_specific
    assert extract_slots("북악관 3층 복도 불이 나갔어요").location_text == "북악관 3층 복도"


def test_light_out_expression_counts_as_problem() -> None:
    # "불이 나갔어요"는 조명 문제 — 상황을 아는 문장이라 "어떤 문제인가요?"를 되묻지 않음
    slots = extract_slots("북악관 3층 복도 불이 나갔어요")
    assert slots.has_problem and slots.category == "전기"


# ── AI 판정 반영 (작업 1-3b) ─────────────────────────────────────────────────
@dataclass
class FakeJudgement:
    category: str = "시설·설비"
    impact: str = "high"
    urgency: str = "low"
    problem_stated: bool = True
    reason: str = "여러 학생이 쓰는 화장실이지만 당장 위험하지는 않아 보여요."


def test_judgement_none_keeps_rule_based_values() -> None:
    slots = extract_slots("혜인관 2층 화장실 물이 계속 새요")
    assert apply_judgement(slots, None, "혜인관 2층 화장실 물이 계속 새요") == slots


def test_judgement_overrides_category_impact_urgency_and_reason() -> None:
    text = "혜인관 2층 화장실 물이 계속 새요"
    slots = apply_judgement(
        extract_slots(text),
        FakeJudgement(category="안전", impact="high", urgency="high", reason="바닥이 젖어 위험해 보여요."),
        text,
    )
    assert slots.category == "안전" and slots.impact == Level.HIGH and slots.urgency == Level.HIGH
    assert slots.building == "혜인관" and slots.floor == "2"  # 위치는 규칙(학교 데이터 검증)이 그대로
    assert judge_reason(slots, "P1") == "바닥이 젖어 위험해 보여요. 그래서 P1로 판단했어요."


def test_judgement_impact_is_low_when_location_unknown() -> None:
    text = "물이 새요"
    slots = apply_judgement(extract_slots(text), FakeJudgement(impact="high"), text)
    assert slots.impact == Level.LOW  # 위치를 전혀 모르면 AI가 high라 해도 저


def test_emergency_word_raises_urgency_and_drops_contradicting_reason() -> None:
    text = "북악관 3층 강의실 콘센트에서 연기가 나요"
    slots = apply_judgement(extract_slots(text), FakeJudgement(urgency="low"), text)
    assert slots.urgency == Level.HIGH and slots.reason is None
    assert "안전 위험 신호가 있어" in judge_reason(slots, "P1")  # 규칙 기반 문구로 대체


def test_judgement_problem_stated_false_means_ask_problem() -> None:
    text = "혜인관 2층 화장실 상태가 그래요"
    slots = apply_judgement(extract_slots(text), FakeJudgement(problem_stated=False), text)
    assert not slots.has_problem
    question = next_question(slots, set())
    assert question is not None and question.key == ASK_PROB


def test_judge_reason_falls_back_to_rules_without_ai_reason() -> None:
    slots = extract_slots("혜인관 2층 화장실 물이 계속 새요")
    assert slots.reason is None
    assert judge_reason(slots, "P2") == "여러 사람이 쓰는 공간이고 급한 위험 신호는 없어 P2로 판단했어요."


# ── 예외처리 보완 (엘리베이터·여러 건·불확실한 위치·없는 시설) ──────────────────────────────────
def _history_until_question(first: str, kind: str, question: str) -> list[Msg]:
    """첫 신고 → 접수 제안 → "네" → (kind) 되묻기까지의 저장된 대화."""
    return [
        Msg(U, first, REPORT),
        Msg(A, "접수를 도와드릴까요?", REPORT, debug_payload={"kind": "offer"}),
        Msg(U, "네, 접수해 주세요", REPORT),
        Msg(A, question, REPORT, debug_payload={"kind": kind}),
    ]


@pytest.mark.parametrize("text", ["유담관 엘리베이터 고장났어요", "혜인관 3층 엘리베이터가 멈췄어요", "청운관 엘베 고장"])
def test_elevator_needs_only_building(text: str) -> None:
    """엘리베이터는 건물 전체 설비 — 건물만 알면 층·호수를 묻지 않고 바로 요약으로 간다."""
    assert next_question(extract_slots(text), set()) is None


def test_elevator_without_building_asks_building_only() -> None:
    for text in ("엘리베이터가 고장났어요", "엘베 고장났어요"):
        q = next_question(extract_slots(text), set())
        assert q is not None
        assert q.key == ASK_LOC
        assert "층" not in q.text
        assert "엘리베이터" in q.text


def test_elevator_nickname_is_understood() -> None:
    slots = extract_slots("북악관 엘베 고장났어요")
    assert slots.detail == "엘리베이터"
    assert slots.category == "시설·설비"


def test_other_places_still_ask_floor() -> None:
    q = next_question(extract_slots("청운관 화장실 변기가 막혔어요"), set())
    assert q is not None and "몇 층" in q.text


def test_dirty_word_variant_is_cleaning() -> None:
    assert extract_slots("본관 화장실이 너무 더럽습니다").category == "청소·위생"


@pytest.mark.parametrize(
    "text",
    [
        "청운관 2층 정수기가 고장났어요. 물도 안 나와요",  # 같은 정수기의 두 증상
        "북악관 화장실에 냄새나고 더러워요",
        "정수기가 고장났어요. 3층이요",
        "혜인관 2층 정수기가 고장났어요",
        "유담관 2층 정수기가 고장났다고 신고했는데 알고 보니 유담관에는 정수기가 없어요",
    ],
)
def test_single_issue_has_no_ambiguity(text: str) -> None:
    assert find_ambiguity(text) is None


def test_multiple_issues_are_split_not_merged() -> None:
    amb = find_ambiguity("청운관 2층 정수기가 고장났고 북악관 2층 화장실 변기도 막혔어요.")
    assert amb is not None and amb.kind == "issues"
    assert amb.options == ["청운관 2층 정수기가 고장났고", "북악관 2층 화장실 변기도 막혔어요"]
    q = next_question(extract_slots("x"), set(), ambiguity=amb)
    assert q is not None and q.key == KIND_SPLIT and q.choices == amb.options


def test_three_issues_and_request_tail_is_removed() -> None:
    text = (
        "청운관 4층 정수기가 고장났고 북악관 1층 화장실도 막혔어요. 그리고 혜인관 옆 스타벅스 앞 "
        "가로등도 꺼져있는데 세 개 다 접수해주세요."
    )
    amb = find_ambiguity(text)
    assert amb is not None and amb.kind == "issues" and len(amb.options) == 3
    assert all("접수해주세요" not in o for o in amb.options)


def test_later_issue_inherits_building_of_earlier_one() -> None:
    amb = find_ambiguity("북악관 1층 화장실에 쓰레기가 많고 2층 자판기도 고장났어요.")
    assert amb is not None and amb.kind == "issues"
    second = extract_slots(amb.extracts[1])
    assert (second.building, second.floor) == ("북악관", "2")  # 없는 "2층 화장실"을 지어내지 않음


def test_two_buildings_in_one_issue_ask_which() -> None:
    amb = find_ambiguity("청운관인지 북악관인지 잘 모르겠는데 정수기가 고장났어요.")
    assert amb is not None and amb.kind == "building" and amb.options == ["청운관", "북악관"]
    q = next_question(extract_slots("x"), set(), ambiguity=amb)
    assert q is not None and q.key == KIND_CHOOSE_B and "잘 모르겠어요" in (q.choices or [])


def test_two_floors_in_one_issue_ask_which() -> None:
    amb = find_ambiguity("북악관 2층이라고 했는데 사실 3층일 수도 있어요.")
    assert amb is not None and amb.kind == "floor" and amb.options == ["2층", "3층"]


def test_split_answer_keeps_only_the_chosen_issue() -> None:
    first = "청운관 2층 정수기가 고장났고 북악관 2층 화장실 변기도 막혔어요."
    amb = find_ambiguity(first)
    assert amb is not None
    draft = collect_draft(_history_until_question(first, KIND_SPLIT, "한 번에 한 건씩 접수할 수 있어요."))
    assert ASK_AMB in draft.asked
    extract, desc = draft.with_reply(amb.options[1])
    slots = extract_slots("\n".join(extract))
    assert (slots.building, slots.floor, slots.detail) == ("북악관", "2", "화장실")
    assert "청운관" not in "\n".join(extract) and "정수기" not in "\n".join(desc)
    assert find_ambiguity(extract[0]) is None


def test_split_answer_by_ordinal_word() -> None:
    first = "청운관 2층 정수기가 고장났고 북악관 2층 화장실 변기도 막혔어요."
    draft = collect_draft(_history_until_question(first, KIND_SPLIT, "q"))
    extract, _ = draft.with_reply("첫 번째 거요")
    assert extract_slots("\n".join(extract)).building == "청운관"


def test_unrecognized_split_answer_asks_once_more_then_gives_up() -> None:
    first = "청운관 2층 정수기가 고장났고 북악관 2층 화장실 변기도 막혔어요."
    amb = find_ambiguity(first)
    assert amb is not None
    draft = collect_draft(_history_until_question(first, KIND_SPLIT, "q"))
    draft.with_reply("전부 다요")
    assert next_question(extract_slots(first), draft.asked, ambiguity=amb).key == KIND_SPLIT  # type: ignore[union-attr]
    again = {ASK_AMB, f"{ASK_AMB}#2"}
    q = next_question(extract_slots(first), again, ambiguity=amb)
    assert q is None or q.key != KIND_SPLIT


def test_building_answer_masks_the_other_candidate() -> None:
    first = "청운관인지 북악관인지 잘 모르겠는데 정수기가 고장났어요."
    draft = collect_draft(_history_until_question(first, KIND_CHOOSE_B, "q"))
    extract, desc = draft.with_reply("청운관")
    slots = extract_slots("\n".join(extract))
    assert slots.building == "청운관" and not slots.location_uncertain
    assert "북악관" in "\n".join(desc)  # 상황 기록(description)엔 학생이 한 말을 그대로 둠


def test_building_unsure_answer_does_not_guess() -> None:
    first = "청운관인지 북악관인지 잘 모르겠는데 정수기가 고장났어요."
    draft = collect_draft(_history_until_question(first, KIND_CHOOSE_B, "q"))
    extract, _ = draft.with_reply("잘 모르겠어요")
    slots = extract_slots("\n".join(extract))
    assert slots.building is None  # 둘 중 하나를 임의로 고르지 않음
    assert next_question(slots, draft.asked, unsure=True) is None  # 더 묻지 않고 요약(담당자가 확인)으로


def test_floor_answer_bare_number_and_confirmed() -> None:
    first = "북악관 2층이라고 했는데 사실 3층일 수도 있어요."
    draft = collect_draft(_history_until_question(first, KIND_CHOOSE_F, "q"))
    extract, _ = draft.with_reply("3")
    slots = extract_slots("\n".join(extract))
    assert (slots.building, slots.floor) == ("북악관", "3")
    assert not slots.location_uncertain  # 학생이 골랐으므로 확정


@pytest.mark.parametrize(
    "text",
    [
        "본관인 것 같은데 정확한 건물은 모르겠어요",
        "은주관 3층 화장실이라고 들었는데 직접 확인하지는 못했어요",
        "북악관 3층일 수도 있어요 화장실 막혔어요",
    ],
)
def test_uncertain_location_is_flagged_not_asserted(text: str) -> None:
    slots = extract_slots(text)
    assert slots.location_uncertain
    assert (slots.location_text or "").endswith("(학생도 확실하지 않음)")
    assert "담당자가 다시 확인할게요" in build_summary(slots, [text])


@pytest.mark.parametrize(
    "text",
    ["청운관 정수기가 고장난 것 같아요", "강의실 에어컨 고장난듯?", "혜인관 2층 정수기 고장난 거 같아여", "본관 맞아요 본관인 것 같은데"],
)
def test_uncertainty_about_the_problem_is_not_location_uncertainty(text: str) -> None:
    assert not extract_slots(text).location_uncertain


def test_unknown_floor_is_not_asked_or_invented() -> None:
    text = "북악관 에어컨이 고장났어요. 정확한 층은 모르겠고 사진도 없어요. 그냥 아무 층이나 접수해주세요."
    slots = extract_slots(text)
    assert slots.floor is None and slots.floor_unknown
    q = next_question(slots, set())
    assert q is None or q.key != "floor"


def test_unknown_facility_asks_once_then_notes_in_summary() -> None:
    text = "청운관 2층에 있는 수영장이 너무 더러워요."
    slots = extract_slots(text)
    assert slots.unknown_facility == "수영장"
    q = next_question(slots, set())
    assert q is not None and q.key == KIND_FACILITY and "수영장 맞아요" in (q.choices or [])
    after = next_question(slots, {ASK_FAC})  # 이미 물었으면 다시 묻지 않음
    assert after is None or after.key != KIND_FACILITY
    assert "수영장" in build_summary(slots, [text], affirmed=True)
    confirmed = next_question(slots, set(), affirmed=True)  # "수영장 맞아요"
    assert confirmed is None or confirmed.key != KIND_FACILITY


def test_known_facilities_are_not_flagged() -> None:
    assert extract_slots("유담관 스포렉스 샤워실 물이 안 나와요").unknown_facility is None
    assert extract_slots("혜인관 2층 정수기가 고장났어요").unknown_facility is None


def test_facility_question_comes_after_building_question() -> None:
    q = next_question(extract_slots("은주관 7층 수영장에 있는 정수기가 고장났어요"), set())
    assert q is not None and q.key == ASK_LOC  # 은주1관/2관부터


# ── 방 안에 엘리베이터 (없는 것 같은 조합은 한 번 확인) ─────────────────────────────────
NESTED = "혜인관 2층 강의실 안에 있는 엘리베이터가 고장났어요."


def test_elevator_inside_classroom_is_questioned_not_filed() -> None:
    slots = extract_slots(NESTED)
    assert slots.nested_facility == ("강의실", "엘리베이터")
    assert slots.detail == "엘리베이터"  # 없는 "강의실 호수"를 묻지 않음
    q = next_question(slots, set())
    assert q is not None and q.key == KIND_NESTED
    assert "강의실 안이 맞아요" in (q.choices or []) and "복도" in (q.choices or [])


@pytest.mark.parametrize(
    "text", ["혜인관 2층 강의실 앞 엘리베이터가 고장났어요", "강의실에서 엘리베이터까지 가는 길 바닥이 미끄러워요"]
)
def test_room_next_to_elevator_is_not_nested(text: str) -> None:
    assert extract_slots(text).nested_facility is None


def test_nested_answer_hallway_becomes_the_location() -> None:
    draft = collect_draft(_history_until_question(NESTED, KIND_NESTED, "q"))
    assert ASK_NST in draft.asked
    extract, _ = draft.with_reply("복도")
    slots = extract_slots("\n".join(extract))
    assert (slots.building, slots.floor, slots.detail) == ("혜인관", "2", "복도")
    after = next_question(slots, draft.asked)
    assert after is None or after.key != KIND_NESTED


def test_nested_unsure_answer_keeps_elevator_and_does_not_ask_room_number() -> None:
    draft = collect_draft(_history_until_question(NESTED, KIND_NESTED, "q"))
    extract, _ = draft.with_reply("잘 모르겠어요")
    slots = extract_slots("\n".join(extract))
    assert slots.detail == "엘리베이터"
    assert next_question(slots, draft.asked, unsure=True) is None


def test_nested_insisted_is_accepted_with_note() -> None:
    draft = collect_draft(_history_until_question(NESTED, KIND_NESTED, "q"))
    extract, _ = draft.with_reply("강의실 안이 맞아요")
    slots = extract_slots("\n".join(extract))
    assert slots.detail == "강의실"  # 학생이 고집하면 말한 대로 (호수를 묻는 흐름으로 이어짐)
    assert "학교 정보와 달라서" in build_summary(slots, [NESTED], affirmed=True)
