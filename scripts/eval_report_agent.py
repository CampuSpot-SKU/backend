"""신고 접수 에이전트 평가 — 실제 Gemini로 케이스를 돌려 기대 동작과 비교한다 (작업 1-3f).

실행: GEMINI_API_KEY=... python scripts/eval_report_agent.py [--only 18,S4] [--group 흐름] [--out report.md]
필요: `ai` 패키지가 설치돼 있어야 함 (pip install git+https://github.com/CampuSpot-SKU/ai.git 또는 pip install -e ../ai).
운영 DB·배포 서버는 건드리지 않는다 (에이전트 함수를 직접 호출).
"""
import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_eval_cases import CASES, Case  # noqa: E402
from agent_extra_cases import EXTRA  # noqa: E402

from ai.agent import AgentError, BuildingInfo, Candidate, TurnMessage, TurnRequest, turn  # noqa: E402
from app.services import report_agent as ra  # noqa: E402
from app.services.ai_client import AgentState, AgentTurn, AiServiceError, HistoryItem  # noqa: E402
from app.services.slot_filling import campus_rules  # noqa: E402

MAX_QUESTIONS = 2


def agent_via_ai(conv, state, prev, left, buildings, candidates, hints):  # type: ignore[no-untyped-def]
    """backend의 report_turn 대신 ai 에이전트를 직접 호출 (HTTP 없이) — 나머지 정책은 backend 코드 그대로."""
    req = TurnRequest(
        conversation=[TurnMessage(role=h.role, content=h.content) for h in conv],
        state=state.model_dump() if state else None,
        prev_action=prev,
        questions_left=left,
        buildings=[BuildingInfo(**b) for b in buildings],
        candidates=[Candidate(**c) for c in candidates],
        hints=hints,
    )
    r = turn(req)
    return AgentTurn(action=r.action, message=r.message, choices=r.choices, state=AgentState(**r.state.model_dump()))


def run_case(case: Case) -> dict[str, Any]:
    """학생 발화를 차례로 보내며 backend 정책(decide)을 거친 결과를 모은다. 확인 뒤 "네"류 답은 접수로 본다."""
    names: list[str] = list(campus_rules()["buildings"])
    tail = ra.Tail(conversation=[], state=None, prev_action=None, asked=0, student_turns=0, user_texts=[])
    transcript: list[str] = []
    result: ra.Decision | None = None
    error = ""
    for text in case["turns"]:
        transcript.append(f"학생: {text}")
        try:
            result = ra.decide(tail, text, names, agent_via_ai)
        except (AgentError, AiServiceError) as e:
            error = f"AgentError: {str(e)[:120]}"
            break
        tail.conversation += [HistoryItem(role="user", content=text), HistoryItem(role="assistant", content=result.message or "(접수)")]
        tail.user_texts.append(text)
        tail.student_turns += 1
        tail.state, tail.prev_action, tail.prev_clarify = result.state, result.action, result.clarify
        if result.action == "ask":
            tail.asked += 1
        chips = f" {result.choices}" if result.choices else ""
        transcript.append(f"챗봇[{result.action}]: {result.message}{chips}")
    return {"case": case, "result": result, "error": error, "transcript": transcript, "asked": tail.asked}


def check(case: Case, result: ra.Decision | None, asked: int) -> list[str]:
    exp = case["expect"]
    if result is None:
        return ["응답 없음"]
    problems: list[str] = []
    st = result.state
    msg = result.message
    if "action" in exp and result.action not in exp["action"]:
        problems.append(f"action={result.action} (기대 {exp['action']})")
    if "building" in exp and st.building != exp["building"]:
        problems.append(f"building='{st.building}' (기대 '{exp['building']}')")
    if "floor" in exp and st.floor != exp["floor"]:
        problems.append(f"floor='{st.floor}' (기대 '{exp['floor']}')")
    if exp.get("certain_not") and st.location_certainty == "confirmed":
        problems.append("불확실한 위치를 confirmed로 처리")
    if "plausible" in exp and st.plausible != exp["plausible"]:
        problems.append(f"plausible={st.plausible}")
    if "problem_clear" in exp and st.problem_clear != exp["problem_clear"]:
        problems.append(f"problem_clear={st.problem_clear}")
    for bad in exp.get("no_msg", []):
        if bad in msg:
            problems.append(f"금지 표현 '{bad}'")
    if exp.get("any_msg") and not any(w in msg for w in exp["any_msg"]):
        problems.append(f"기대 표현 없음 {exp['any_msg']}")
    if len(result.choices) < exp.get("choices_min", 0):
        problems.append(f"choices {len(result.choices)}개")
    if len(st.pending_issues) < exp.get("pending_min", 0):
        problems.append(f"pending_issues {len(st.pending_issues)}개")
    if "area" in exp and st.area not in exp["area"]:
        problems.append(f"area='{st.area}' (기대 {exp['area']})")
    if "room_no" in exp and st.room_no != exp["room_no"]:
        problems.append(f"room_no='{st.room_no}' (기대 '{exp['room_no']}')")
    if "room_kind" in exp and st.room_kind != exp["room_kind"]:
        problems.append(f"room_kind='{st.room_kind}' (기대 '{exp['room_kind']}')")
    for nb in exp.get("near", []):
        if nb not in st.near:
            problems.append(f"near에 '{nb}' 없음")
    if exp.get("staff_check") and not st.staff_check:
        problems.append("staff_check 비어 있음")
    if exp.get("no_ask_building") and ("어느 건물" in msg or "어떤 건물" in msg or "건물이 어디" in msg):
        problems.append("건물을 되묻음")
    if asked > MAX_QUESTIONS:
        problems.append(f"질문 {asked}번 (상한 {MAX_QUESTIONS})")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="쉼표로 구분한 케이스 id")
    ap.add_argument("--group", default="")
    ap.add_argument("--set", default="main", choices=["main", "extra", "all"], help="평가 케이스 묶음")
    ap.add_argument("--out", default="")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    cases = [
        c for c in {"main": CASES, "extra": EXTRA, "all": CASES + EXTRA}[args.set]
        if (not args.only or c["id"] in args.only.split(",")) and (not args.group or c["group"] == args.group)
    ]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        runs = list(pool.map(run_case, cases))
    by_group: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    lines: list[str] = []
    failed: list[str] = []
    for run in runs:
        case = run["case"]
        problems = ([run["error"]] if run["error"] else []) + check(case, run["result"], run["asked"])
        ok = not problems
        by_group[case["group"]][0 if ok else 1] += 1
        if not ok:
            failed.append(case["id"])
        lines.append(f"### {'PASS' if ok else 'FAIL'} {case['id']} ({case['group']})")
        lines += [f"    {t}" for t in run["transcript"]]
        if problems:
            lines.append("    ✗ " + " / ".join(problems))
    total_ok = sum(v[0] for v in by_group.values())
    total = sum(sum(v) for v in by_group.values())
    summary = [f"통과 {total_ok}/{total}"] + [f"  {g}: {v[0]}/{sum(v)}" for g, v in by_group.items()]
    if failed:
        summary.append("실패: " + ", ".join(failed))
    text = "\n".join(summary + [""] + lines)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(json.dumps({"passed": total_ok, "total": total}), file=sys.stderr)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
