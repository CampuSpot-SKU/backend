"""슬롯 추출·대화 재구성 유닛테스트 — DB/네트워크 없이 실행 (작업 1-3)."""
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from app.models.enums import ChatIntent, ChatRole, Level
from app.services.slot_filling import (
    ASK_LOCATION,
    ASK_PROBLEM,
    CANCEL_HINT,
    BuildingRef,
    collect_draft,
    extract_slots,
    is_cancel,
    next_question,
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


# ── next_question ───────────────────────────────────────────────────────────
def test_next_question_order_and_asks_only_once() -> None:
    empty = extract_slots("저기요")
    assert next_question(empty, set()) == ASK_LOCATION
    assert next_question(empty, {ASK_LOCATION}) == ASK_PROBLEM
    assert next_question(empty, {ASK_LOCATION, ASK_PROBLEM}) is None  # 더 안 묻고 접수
    assert next_question(extract_slots("3동 화장실 물이 새요"), set()) is None


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
    assert draft.asked == {ASK_LOCATION}
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
