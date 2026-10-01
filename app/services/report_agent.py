"""신고 접수 대화 에이전트 연결부 (명세서 4-1, 작업 1-3f).

말을 이해하는 일은 ai의 에이전트(Gemini)가 하고, 여기서는 (1) 학교 데이터로 근거를 만들어 건네고
(2) 돌아온 결과를 정책에 맞게 다듬고 (3) 접수에 쓸 슬롯으로 바꾼다. 정책은 에이전트가 뭐라 하든 지킨다:
- 되묻기는 신고 하나당 MAX_QUESTIONS번까지, 학생 발화는 MAX_STUDENT_TURNS번까지
- 접수(submit)는 직전 행동이 확인(confirm)일 때만 — 학생이 확인에 답하기 전에는 접수하지 않음
- 문제가 분명하지 않으면 확인 단계로 가지 않음
대화 상태는 DB 메시지의 debug_payload({"kind": "agent", "action": ..., "state": ...})에서 되살린다.
"""
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from app.models.enums import ChatIntent, ChatRole
from app.services import campus_places as cp
from app.services.ai_client import AgentState, AgentTurn, HistoryItem
from app.services.slot_filling import (
    BuildingRef,
    ReportSlots,
    _building_ref,
    _impact,
)

# 의도분류가 잡담·범위 밖으로 본 말이라도 신고 요청이거나 안전이 걸린 말이면 에이전트가 직접 판단하게 한다.
# (정규식은 "말이 무슨 뜻인가"가 아니라 "사람 판단이 필요한 말인가"만 가려낸다 — 판단은 에이전트가 함. 안전 단어는 ai/agent.py와 같이 유지)
_REPORT_REQUEST_RE = re.compile(r"신고|접수")
_SAFETY_RE = re.compile(
    r"화재|불이\s*(?:났|붙)|불났|연기가\s*(?:나|자욱|가득|올라)|연기\s*나|가스\s*(?:냄새|누출|샌|새)|감전|폭발|폭파|폭탄|쓰러|의식\s*(?:이\s*)?없|(?<![가-힣])피가\s*(?:나|난|흘|철철|많)|피를\s*(?:흘|토|많)|다쳤|다친|갇혔|갇혀|끼였|추락"
)


def needs_agent_review(text: str) -> bool:
    """잡담·범위 밖으로 분류됐지만 신고 요청("신고해주세요")이거나 위험한 말이면 에이전트에게 넘긴다."""
    return bool(_REPORT_REQUEST_RE.search(text) or _SAFETY_RE.search(text))


AGENT_KIND = "agent"
DECLINE_KIND = "agent_decline"  # 에이전트가 거절한 말 — 학생이 다시 말하면 이어서 봄
MAX_QUESTIONS = 2
MAX_STUDENT_TURNS = 10
CONFIRM_CHOICES = ["네, 접수해 주세요", "내용을 고칠래요", "취소할게요"]
GIVE_UP = "신고 내용을 정확히 알기 어려워서 접수는 하지 않을게요. 어떤 시설에 무슨 문제가 있는지 알려주시면 다시 도와드릴게요."
Agent = Callable[
    [list[HistoryItem], AgentState | None, str | None, int, list[dict[str, str]], list[dict[str, str]], list[str]],
    AgentTurn,
]


@dataclass
class Tail:
    """진행 중인 신고 대화 (끝의 intent=REPORT 구간)."""

    conversation: list[HistoryItem]
    state: AgentState | None
    prev_action: str | None
    asked: int
    student_turns: int
    user_texts: list[str]
    prev_clarify: bool = False  # 직전 챗봇 말이 "별개인가요, 고칠까요?" 확인이었나


def _payload(msg: object) -> dict[str, object]:
    p = getattr(msg, "debug_payload", None)
    return p if isinstance(p, dict) else {}


def is_agent_flow(history: Sequence[object]) -> bool:
    """직전 챗봇 메시지가 에이전트가 보낸 신고 대화인가."""
    if not history:
        return False
    last = history[-1]
    return (
        getattr(last, "role", None) == ChatRole.ASSISTANT
        and getattr(last, "intent", None) == ChatIntent.REPORT
        and _payload(last).get("kind") == AGENT_KIND
    )


def report_tail(history: Sequence[object]) -> Tail:
    """히스토리 끝의 신고 구간에서 에이전트 상태를 되살린다. 이번에 보낸 학생 메시지는 포함하지 않음."""
    i = len(history) - 1
    while i >= 0 and getattr(history[i], "intent", None) == ChatIntent.REPORT:
        i -= 1
    tail = Tail(conversation=[], state=None, prev_action=None, asked=0, student_turns=0, user_texts=[])
    for msg in history[i + 1:]:
        role = getattr(msg, "role", None)
        content = str(getattr(msg, "content", ""))
        if role == ChatRole.USER:
            tail.conversation.append(HistoryItem(role="user", content=content))
            tail.user_texts.append(content)
            tail.student_turns += 1
        else:
            tail.conversation.append(HistoryItem(role="assistant", content=content))
            p = _payload(msg)
            if p.get("kind") == AGENT_KIND:
                action = str(p.get("action") or "")
                tail.prev_action = action or None
                tail.prev_clarify = bool(p.get("clarify"))
                if action == "ask":
                    tail.asked += 1
                raw = p.get("state")
                if isinstance(raw, dict):
                    tail.state = AgentState.model_validate(raw)
            else:
                tail.prev_action = None
                tail.prev_clarify = False
    return tail


def grounding(texts: Sequence[str], building_names: Sequence[str]) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    """에이전트에게 줄 학교 데이터: 건물·층 목록, 학생 말과 닮은 장소 후보, 별칭 풀이."""
    joined = " ".join(texts)
    buildings = [{"name": n, "floors": f} for n, f in cp.building_floor_info(list(building_names))]
    candidates = [
        {"label": h.label, "building": h.building, "floor": h.floor} for h in cp.search_places(joined)
    ]
    return buildings, candidates, cp.term_hints(joined)


def _where(state: AgentState) -> str:
    parts = [state.building or state.location_note]
    if state.floor:
        parts.append(f"지하 {state.floor[1:]}층" if state.floor.startswith("B") else f"{state.floor}층")
    if state.room_no:
        parts.append(f"{state.room_no}호")
    parts.append(state.room_name or state.place)
    if not state.building and state.near:
        parts.insert(0, f"{'·'.join(state.near)} {'사이' if state.near_relation == 'between' else '근처'}")
    return " ".join(p for p in parts if p)


def _confirm_text(state: AgentState) -> str:
    where = _where(state)
    prefix = f"{where}에서 " if where else ""
    return f"{prefix}'{state.problem}' 문제로 정리했어요. 이대로 접수할까요?"


@dataclass
class Decision:
    action: str  # ask / confirm / submit / cancel / decline
    message: str
    choices: list[str]
    state: AgentState
    clarify: bool = False  # 앞 건과 다른 건물을 말해서 별건·정정 확인을 한 경우


_CORRECTION_RE = re.compile(r"아니|잘못|말고|아닌|바꿔|바꿀|고쳐|고칠|정정|수정|틀렸|실수")


def _other_building_clarify(tail: Tail, user_text: str, state: AgentState) -> Decision | None:
    """앞 건과 다른 건물을 고치는 표시 없이 말하면 말없이 덮어쓰지 않고 별건인지 정정인지 한 번 묻는다."""
    old = tail.state.building if tail.state else ""
    new = state.building
    if not old or not new or old == new or tail.prev_clarify or tail.prev_action == "confirm":
        return None
    if _CORRECTION_RE.search(user_text):
        return None
    msg = f"앞에서 '{old}'라고 하셨는데, '{new}'는 별개 건인가요, 아니면 '{old}'를 '{new}'(으)로 고칠까요?"
    return Decision("ask", msg, ["별개 건이에요", "고칠게요"], tail.state or state, clarify=True)


def _fill_landmark_near(state: AgentState, texts: Sequence[str], building_names: Sequence[str]) -> AgentState:
    """학생이 말한 장소가 학교 데이터의 건물 아닌 장소(혜청사 등)면 가까운 건물을 데이터대로 채운다 (모델이 빠뜨려도)."""
    if state.near:
        return state
    for hit in cp.search_places(" ".join(texts)):
        if hit.kind == "landmark" and hit.near:
            near = [n for n in hit.near if n in building_names]
            if near:
                area = state.area if state.area != "unknown" else "outdoor_near"
                return state.model_copy(update={"near": near, "near_relation": hit.relation or "attached", "area": area})
    return state


def decide(tail: Tail, user_text: str, building_names: Sequence[str], agent: Agent) -> Decision:
    """학생의 이번 말까지 반영해 다음 행동을 정한다. AiServiceError는 호출한 쪽에서 처리 (규칙 기반으로 대체)."""
    texts = [*tail.user_texts, user_text]
    conversation = [*tail.conversation, HistoryItem(role="user", content=user_text)]
    buildings, candidates, hints = grounding(texts, building_names)
    questions_left = max(0, MAX_QUESTIONS - tail.asked)
    out = agent(conversation, tail.state, tail.prev_action, questions_left, buildings, candidates, hints)
    state, action, message, choices = out.state, out.action, out.message, list(out.choices)
    state = _fill_landmark_near(state, texts, building_names)

    if action not in ("cancel", "decline") and (clar := _other_building_clarify(tail, user_text, state)):
        return clar
    if tail.student_turns + 1 >= MAX_STUDENT_TURNS and action == "ask":
        action = "confirm" if state.problem_clear else "decline"
    if action == "ask" and questions_left == 0:  # 질문 한도 — 더 묻지 않고 지금까지로 확인하거나 마무리
        action = "confirm" if state.problem_clear else "decline"
        message = ""
    if action == "confirm" and not state.problem_clear:
        if questions_left > 0:
            return Decision("ask", "어떤 문제인지 조금만 더 알려주시겠어요?", [], state)
        return Decision("decline", GIVE_UP, [], state)
    if action == "submit" and tail.prev_action != "confirm":  # 확인 없이 접수 금지
        action = "confirm"
        message = ""
    if action == "confirm":
        message = message or _confirm_text(state)
        if "접수" not in message:
            message = _confirm_text(state)
        choices = choices if 1 <= len(choices) <= 3 else CONFIRM_CHOICES
    if action == "decline" and not message:
        message = GIVE_UP
    return Decision(action, message, choices, state)


def slots_from_state(state: AgentState, buildings: Sequence[BuildingRef]) -> ReportSlots:
    """에이전트 상태 → 접수에 쓰는 슬롯 (카테고리·긴급도는 이후 AI 판정이 채움)."""
    building_id, building = (None, None)
    if state.building:
        building_id, building = _building_ref(state.building, buildings)
    detail_parts = [state.room_name or state.place]
    if state.room_no and state.room_no not in (state.room_name or ""):
        detail_parts.append(f"{state.room_no}호")
    detail = " ".join(p for p in detail_parts if p) or None
    slots = ReportSlots(
        building_id=building_id,
        building=building,
        floor=state.floor or None,
        detail=detail,
        detail_specific=bool(state.room_no or state.room_name),
        has_problem=state.problem_clear,
        unknown_place=None if building else (state.location_note or None),
        location_uncertain=state.location_certainty == "uncertain",
        floor_unknown=not state.floor,
    )
    if not slots.building and state.near:  # 건물이 아닌 곳은 가까운 건물을 위치 글자로 남김
        slots = replace(
            slots, unknown_place=f"{'·'.join(state.near)} {'사이' if state.near_relation == 'between' else '근처'}"
        )
    return replace(slots, impact=_impact(f"{state.problem} {state.place}", slots.building, slots.detail))


def description_for(user_texts: Sequence[str], state: AgentState) -> str:
    """접수 상황(description): 학생이 한 말 + 담당자가 알아야 할 확인 사항."""
    body = state.problem or " ".join(user_texts)
    notes = list(state.staff_check)
    if state.location_note and not state.building:
        notes.append(f"학생이 말한 위치: {state.location_note}")
    if not state.plausible:
        notes.append("내용이 현실과 달라 보임")
    if state.room_no and state.building and state.floor and not cp.find_room(state.building, state.floor, state.room_no):
        notes.append(f"학교 데이터에 {state.room_no}호 없음")
    if notes:
        body += f"\n[챗봇 메모] 담당자 확인: {', '.join(dict.fromkeys(notes))}"
    return body


