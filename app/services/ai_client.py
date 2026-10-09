"""ai 서비스(campuspot-ai) 호출 — 의도분류.

- 주소/인증: AI_SERVICE_URL + X-Internal-Secret(AI_SERVICE_SECRET) (명세서 5-1 "ai 서비스")
- Gemini 재시도는 ai 서비스 안에서 이미 1회 하므로 여기선 재시도하지 않는다.
- 실패하면 AiServiceError → 라우터가 503 "잠시 후 다시 시도해주세요"로 응답 (명세서 11장).
"""
import base64
import logging
from typing import Literal

import httpx
from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.services.pii_mask import mask_optional, mask_pii

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


def _masked(history: list[HistoryItem]) -> list[dict[str, str]]:
    """Gemini로 보내는 대화 기록 — 학번·전화번호 마스킹 (1-23 2겹째: 입구를 우회한 옛 메시지도 새지 않게)."""
    return [{"role": h.role, "content": mask_pii(h.content)} for h in history]


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
        "text": mask_pii(text),
        "history": _masked(history[-MAX_HISTORY:]),
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
            "base_text": mask_pii(base_text),
            "must_include": [mask_pii(m) for m in must_include],
            "history": _masked(history[-4:]),
        }
        headers = {"X-Internal-Secret": get_settings().ai_service_secret}
        res = httpx.post(url, json=payload, headers=headers, timeout=SAY_TIMEOUT_SECONDS)
        res.raise_for_status()
        text = str(res.json().get("text", "")).strip()
    except (AiServiceError, httpx.HTTPError, ValueError) as e:
        logger.info("말투 다듬기 실패 → 고정 문구 사용 (%s): %s", kind, e)
        return base_text
    # 보낸 값과 같은 기준(마스킹된 must_include)으로 확인 — 원본 번호를 다시 요구해 어긋나지 않게
    must = [mask_pii(m) for m in must_include]
    return text if text and all(m in text for m in must) else base_text


JUDGE_TIMEOUT_SECONDS = 10.0  # 판정은 실패해도 규칙 기반으로 대체되므로 오래 기다리지 않음
JUDGE_PHOTO_TIMEOUT_SECONDS = 25.0  # 사진이 붙으면 이미지 전송·분석 때문에 더 걸림 (1-10)


class JudgeResult(BaseModel):
    """ai `POST /api/v1/report/judge` 응답 (1-3b)."""

    category: str
    impact: Literal["high", "low"]
    urgency: Literal["high", "low"]
    problem_stated: bool
    reason: str
    photo_note: str | None = None  # 사진에서 보이는 상황 한 줄 (사진이 없거나 AI가 쓸 수 없으면 없음, 1-10)


PhotoData = tuple[bytes, str]  # (사진 바이트, "image/jpeg" | "image/png")


def judge_report(
    text: str, location: str | None, categories: list[str], photo: PhotoData | None = None
) -> JudgeResult:
    """신고 내용(+사진)의 카테고리·영향도·긴급도·이유를 AI가 판정. 실패하면 AiServiceError (호출한 쪽이 규칙 기반으로 대체)."""
    url = _endpoint("/report/judge")
    payload: dict[str, object] = {
        "text": mask_pii(text)[:2000],
        "location": mask_optional(location),
        "categories": categories,
    }
    if photo is not None:
        payload["photo_base64"] = base64.b64encode(photo[0]).decode("ascii")
        payload["photo_mime"] = photo[1]
    headers = {"X-Internal-Secret": get_settings().ai_service_secret}
    timeout = JUDGE_PHOTO_TIMEOUT_SECONDS if photo is not None else JUDGE_TIMEOUT_SECONDS
    try:
        res = httpx.post(url, json=payload, headers=headers, timeout=timeout)
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


def _masked_state(state: AgentState) -> dict[str, object]:
    """에이전트 상태의 문자열 값(학생 말에서 뽑은 것)도 마스킹. 학교 데이터(buildings·candidates)는 그대로."""
    out: dict[str, object] = {}
    for k, v in state.model_dump().items():
        if isinstance(v, str):
            out[k] = mask_pii(v)
        elif isinstance(v, list):
            out[k] = [mask_pii(x) if isinstance(x, str) else x for x in v]
        else:
            out[k] = v
    return out


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
        "conversation": _masked(conversation[-24:]),
        "state": _masked_state(state) if state else None,
        "prev_action": prev_action,
        "questions_left": questions_left,
        "buildings": buildings,
        "candidates": candidates,
        "hints": [mask_pii(h) for h in hints or []],
    }
    headers = {"X-Internal-Secret": get_settings().ai_service_secret}
    try:
        res = httpx.post(url, json=payload, headers=headers, timeout=AGENT_TIMEOUT_SECONDS)
        res.raise_for_status()
        return AgentTurn.model_validate(res.json())
    except (httpx.HTTPError, ValidationError, ValueError) as e:
        logger.warning("ai report turn 실패: %s", e)
        raise AiServiceError(str(e)) from e


RAG_TIMEOUT_SECONDS = 30.0  # 검색(임베딩) + Gemini 답변 + 재시도 1회. 실패하면 503 (잠시 후 다시 시도)


class RagSource(BaseModel):
    title: str
    article_no: str | None = None
    url: str | None = None


class RagAnswer(BaseModel):
    """ai `POST /api/v1/rag/answer` 응답 (1-4c) — sources는 근거가 없으면 빈 목록."""

    answer: str
    sources: list[RagSource] = []


def rag_answer(question: str) -> RagAnswer:
    """행정 문의에 학칙·안내·공지를 근거로 한 답변. 실패하면 AiServiceError (호출한 쪽이 503)."""
    url = _endpoint("/rag/answer")
    headers = {"X-Internal-Secret": get_settings().ai_service_secret}
    try:
        res = httpx.post(
            url, json={"question": mask_pii(question)[:1000]}, headers=headers, timeout=RAG_TIMEOUT_SECONDS
        )
        res.raise_for_status()
        return RagAnswer.model_validate(res.json())
    except (httpx.HTTPError, ValidationError, ValueError) as e:
        logger.warning("ai rag answer 실패: %s", e)
        raise AiServiceError(str(e)) from e
