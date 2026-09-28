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

    intent: Literal["report", "inquiry", "unclear"]
    report_score: int
    inquiry_score: int
    safety_concern: bool = False
    clarifying_question: str | None = None


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
