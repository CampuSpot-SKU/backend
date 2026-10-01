"""ai 서비스(campuspot-ai) 호출 — 의도분류.

- 주소/인증: AI_SERVICE_URL + X-Internal-Secret(AI_SERVICE_SECRET) (명세서 5-1 "ai 서비스")
- Gemini 재시도는 ai 서비스 안에서 이미 1회 하므로 여기선 재시도하지 않는다.
- 실패하면 AiServiceError → 라우터가 503 "잠시 후 다시 시도해주세요"로 응답 (명세서 11장).
"""
import logging
from typing import Literal

import httpx
from pydantic import BaseModel, ValidationError

from app.config import get_settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15.0  # Gemini 호출 + 재시도 1회를 감안한 여유
MAX_HISTORY = 3  # 명세서 5-1: 최근 대화 3개만 맥락으로 사용


class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class IntentResult(BaseModel):
    """ai `POST /api/v1/intent/classify` 응답."""

    intent: Literal["report", "inquiry", "unclear", "chitchat", "off_topic"]
    report_score: int
    inquiry_score: int
    safety_concern: bool = False
    clarifying_question: str | None = None
    talk: str = "none"  # chitchat·off_topic일 때: greeting/thanks/bye/smalltalk/about/off_topic


class AiServiceError(Exception):
    """ai 서비스 호출 실패 (설정 누락·네트워크·5xx·응답 형식 오류 모두)."""


def _endpoint(path: str) -> str:
    base = get_settings().ai_service_url.rstrip("/")
    if not base:
        raise AiServiceError("AI_SERVICE_URL이 설정되지 않았습니다.")
    if not base.endswith("/api/v1"):
        base += "/api/v1"
    return base + path


def classify_intent(text: str, history: list[HistoryItem]) -> IntentResult:
    """발화를 신고/행정문의/애매함으로 분류."""
    url = _endpoint("/intent/classify")
    payload = {
        "text": text,
        "history": [h.model_dump() for h in history[-MAX_HISTORY:]],
    }
    headers = {"X-Internal-Secret": get_settings().ai_service_secret}
    try:
        res = httpx.post(url, json=payload, headers=headers, timeout=TIMEOUT_SECONDS)
        res.raise_for_status()
        return IntentResult.model_validate(res.json())
    except (httpx.HTTPError, ValidationError, ValueError) as e:
        logger.warning("ai intent classify 실패: %s", e)
        raise AiServiceError(str(e)) from e


SAY_TIMEOUT_SECONDS = 5.0  # 말투 다듬기는 없어도 되는 기능 — 오래 기다리지 않고 고정 문구로 넘어감


def say_text(kind: str, base_text: str, must_include: list[str], history: list[HistoryItem]) -> str:
    """신고 대화 문구를 Gemini가 자연스러운 말투로 다듬은 결과. 실패하면 base_text를 그대로 돌려줌.

    뜻·순서는 backend 코드가 정한 그대로고 ai는 말투만 바꾼다 (ai `POST /api/v1/report/say`).
    """
    try:
        url = _endpoint("/report/say")
        payload = {
            "kind": kind,
            "base_text": base_text,
            "must_include": must_include,
            "history": [h.model_dump() for h in history[-4:]],
        }
        headers = {"X-Internal-Secret": get_settings().ai_service_secret}
        res = httpx.post(url, json=payload, headers=headers, timeout=SAY_TIMEOUT_SECONDS)
        res.raise_for_status()
        text = str(res.json().get("text", "")).strip()
    except (AiServiceError, httpx.HTTPError, ValueError) as e:
        logger.info("말투 다듬기 실패 → 고정 문구 사용 (%s): %s", kind, e)
        return base_text
    return text if text and all(m in text for m in must_include) else base_text


JUDGE_TIMEOUT_SECONDS = 10.0  # 판정은 실패해도 규칙 기반으로 대체되므로 오래 기다리지 않음


class JudgeResult(BaseModel):
    """ai `POST /api/v1/report/judge` 응답 (1-3b)."""

    category: str
    impact: Literal["high", "low"]
    urgency: Literal["high", "low"]
    problem_stated: bool
    reason: str


def judge_report(text: str, location: str | None, categories: list[str]) -> JudgeResult:
    """신고 내용의 카테고리·영향도·긴급도·이유를 AI가 판정. 실패하면 AiServiceError (호출한 쪽이 규칙 기반으로 대체)."""
    url = _endpoint("/report/judge")
    payload = {"text": text[:2000], "location": location, "categories": categories}
    headers = {"X-Internal-Secret": get_settings().ai_service_secret}
    try:
        res = httpx.post(url, json=payload, headers=headers, timeout=JUDGE_TIMEOUT_SECONDS)
        res.raise_for_status()
        return JudgeResult.model_validate(res.json())
    except (httpx.HTTPError, ValidationError, ValueError) as e:
        logger.warning("ai report judge 실패: %s", e)
        raise AiServiceError(str(e)) from e


AGENT_TIMEOUT_SECONDS = 25.0  # 신고 대화 에이전트 — Gemini 호출 + 재시도 1회. 실패하면 규칙 기반 흐름으로 대체


class AgentState(BaseModel):
    """ai `POST /api/v1/report/turn`의 신고 상태 (ai/agent.py AgentState와 같은 모양)."""

    problem: str = ""
    symptom: str = ""
    problem_clear: bool = False
    plausible: bool = True
    building: str = ""
    floor: str = ""
    place: str = ""
    location_note: str = ""
    location_certainty: Literal["confirmed", "uncertain", "unknown"] = "unknown"
    area: Literal["unknown", "indoor", "outdoor_near", "outdoor_open", "non_building"] = "unknown"
    room_kind: Literal["unknown", "numbered", "unnumbered"] = "unknown"
    room_no: str = ""
    room_name: str = ""
    contained_in: str = ""
    near: list[str] = []
    near_relation: Literal["none", "attached", "apart", "between"] = "none"
    staff_check: list[str] = []
    pending_issues: list[str] = []


class AgentTurn(BaseModel):
    action: Literal["ask", "confirm", "submit", "cancel", "decline"]
    message: str
    choices: list[str]
    state: AgentState


def report_turn(
    conversation: list[HistoryItem],
    state: AgentState | None,
    prev_action: str | None,
    questions_left: int,
    buildings: list[dict[str, str]],
    candidates: list[dict[str, str]],
    hints: list[str] | None = None,
) -> AgentTurn:
    """신고 접수 대화 한 턴을 에이전트가 처리. 실패하면 AiServiceError (호출한 쪽이 규칙 기반 흐름으로 대체)."""
    url = _endpoint("/report/turn")
    payload = {
        "conversation": [h.model_dump() for h in conversation[-24:]],
        "state": state.model_dump() if state else None,
        "prev_action": prev_action,
        "questions_left": questions_left,
        "buildings": buildings,
        "candidates": candidates,
        "hints": hints or [],
    }
    headers = {"X-Internal-Secret": get_settings().ai_service_secret}
    try:
        res = httpx.post(url, json=payload, headers=headers, timeout=AGENT_TIMEOUT_SECONDS)
        res.raise_for_status()
        return AgentTurn.model_validate(res.json())
    except (httpx.HTTPError, ValidationError, ValueError) as e:
        logger.warning("ai report turn 실패: %s", e)
        raise AiServiceError(str(e)) from e
