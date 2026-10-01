"""신고 대화 에이전트 정책 유닛테스트 — DB·Gemini 없이 (CI용)."""
from types import SimpleNamespace

from app.models.enums import ChatIntent, ChatRole
from app.services import report_agent as ra
from app.services.ai_client import AgentState, AgentTurn
from app.services.slot_filling import BuildingRef

NAMES = ["북악관", "청운관", "혜인관"]


def _msg(role: ChatRole, content: str, payload: dict[str, object] | None = None, intent: ChatIntent | None = ChatIntent.REPORT):
    return SimpleNamespace(role=role, content=content, debug_payload=payload, intent=intent)


def _agent(**kw: object):
    def run(conv, state, prev, left, buildings, candidates, hints):  # type: ignore[no-untyped-def]
        run.calls.append({"left": left, "prev": prev, "candidates": candidates, "hints": hints})  # type: ignore[attr-defined]
        base: dict[str, object] = {"action": "ask", "message": "어떤 문제인가요?", "choices": [],
                                   "state": AgentState(problem="불이 안 켜져요", problem_clear=True)}
        base.update(kw)
        return AgentTurn.model_validate(base)

    run.calls = []  # type: ignore[attr-defined]
    return run


def _tail(asked: int = 0, prev: str | None = None, turns: int = 1) -> ra.Tail:
    return ra.Tail(conversation=[], state=None, prev_action=prev, asked=asked, student_turns=turns, user_texts=["x"] * turns)


def test_tail_rebuilds_state_and_counts_questions() -> None:
    state = AgentState(problem="불이 안 켜져요", building="북악관").model_dump()
    history = [
        _msg(ChatRole.USER, "장문수 교수실 불이 안 켜져"),
        _msg(ChatRole.ASSISTANT, "몇 층인가요?", {"kind": "agent", "action": "ask", "state": state}),
        _msg(ChatRole.USER, "모르겠어요"),
        _msg(ChatRole.ASSISTANT, "이대로 접수할까요?", {"kind": "agent", "action": "confirm", "state": state}),
    ]
    assert ra.is_agent_flow(history)
    tail = ra.report_tail(history)
    assert tail.asked == 1 and tail.prev_action == "confirm" and tail.student_turns == 2
    assert tail.state is not None and tail.state.building == "북악관"
    assert [h.role for h in tail.conversation] == ["user", "assistant", "user", "assistant"]


def test_tail_ignores_messages_before_flow_ended() -> None:
    history = [
        _msg(ChatRole.USER, "예전 신고"),
        _msg(ChatRole.ASSISTANT, "접수됐어요", None, intent=None),
        _msg(ChatRole.USER, "새 신고"),
    ]
    assert ra.report_tail(history).user_texts == ["새 신고"]
    assert not ra.is_agent_flow(history)


def test_grounding_uses_school_data() -> None:
    _, candidates, hints = ra.grounding(["장문수 교수실 불이 안 켜져", "엘베도 이상해요"], NAMES)
    assert any(c["building"] == "북악관" and c["floor"] == "6" for c in candidates)
    assert any("엘베" in h and "엘리베이터" in h for h in hints)


def test_question_budget_is_enforced() -> None:
    out = ra.decide(_tail(asked=2), "그냥요", NAMES, _agent(action="ask"))
    assert out.action == "confirm" and "접수" in out.message and out.choices == ra.CONFIRM_CHOICES


def test_budget_exhausted_and_problem_unclear_ends_politely() -> None:
    state = AgentState(problem="", problem_clear=False)
    out = ra.decide(_tail(asked=2), "이상해요", NAMES, _agent(action="ask", state=state))
    assert out.action == "decline" and out.message == ra.GIVE_UP


def test_submit_requires_prior_confirm() -> None:
    out = ra.decide(_tail(prev="ask"), "네", NAMES, _agent(action="submit", message=""))
    assert out.action == "confirm"
    out = ra.decide(_tail(prev="confirm"), "네", NAMES, _agent(action="submit", message=""))
    assert out.action == "submit"


def test_confirm_needs_clear_problem() -> None:
    state = AgentState(problem="", problem_clear=False)
    out = ra.decide(_tail(), "3층이요", NAMES, _agent(action="confirm", message="접수할까요?", state=state))
    assert out.action == "ask"


def test_student_turn_cap() -> None:
    out = ra.decide(_tail(turns=ra.MAX_STUDENT_TURNS - 1), "음", NAMES, _agent(action="ask"))
    assert out.action == "confirm"


def test_slots_from_state_maps_location() -> None:
    from uuid import uuid4

    bid = uuid4()
    state = AgentState(
        problem="불이 안 켜져요", problem_clear=True, building="북악관", floor="6", room_no="606",
        room_name="장문수 교수연구실", location_certainty="confirmed",
    )
    slots = ra.slots_from_state(state, [BuildingRef(id=bid, name="북악관", aliases=())])
    assert slots.building_id == bid and slots.floor == "6" and slots.has_problem
    assert slots.detail_specific and "606호" in (slots.detail or "")
    assert "북악관" in (slots.location_text or "") and "6층" in (slots.location_text or "")


def test_slots_for_unknown_place_keep_student_words() -> None:
    state = AgentState(problem="의자가 부서졌어요", problem_clear=True, location_note="미래관", place="의자")
    slots = ra.slots_from_state(state, [])
    assert slots.building_id is None and "미래관" in (slots.location_text or "")


def test_slots_for_outdoor_near_buildings() -> None:
    state = AgentState(
        problem="가로등이 꺼졌어요", problem_clear=True, area="outdoor_open",
        near=["혜인관", "청운관"], near_relation="between", place="가로등",
    )
    slots = ra.slots_from_state(state, [])
    assert "혜인관·청운관 사이" in (slots.location_text or "")


def test_description_carries_staff_notes() -> None:
    state = AgentState(problem="정수기에서 커피가 나와요", problem_clear=True, plausible=False, staff_check=["내용 확인"])
    desc = ra.description_for(["정수기에서 커피가 나와요"], state)
    assert "[챗봇 메모]" in desc and "내용 확인" in desc and "현실과 달라" in desc


def test_other_building_without_correction_asks_instead_of_overwriting() -> None:
    old = AgentState(problem="", building="청운관")
    tail = ra.Tail(conversation=[], state=old, prev_action="ask", asked=1, student_turns=1, user_texts=["청운관이 이상해"])
    new = AgentState(problem="", building="북악관")
    out = ra.decide(tail, "북악관이 이상해", NAMES, _agent(action="ask", state=new))
    assert out.clarify and out.state.building == "청운관" and "청운관" in out.message and "북악관" in out.message
    # 고치는 말이 있으면 정정으로 받음
    out = ra.decide(tail, "아니 북악관이요", NAMES, _agent(action="ask", state=new))
    assert not out.clarify and out.state.building == "북악관"
    # 이미 확인했으면 다시 묻지 않음
    tail.prev_clarify = True
    assert not ra.decide(tail, "북악관이 이상해", NAMES, _agent(action="ask", state=new)).clarify
