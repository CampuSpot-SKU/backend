"""챗봇 레이트 리미팅 (명세서 11장: slowapi로 세션당 요청 제한).

키는 URL의 session_id — Cloud Run 뒤에서는 클라이언트 IP가 정확하지 않아서 IP 대신 세션 기준.
한도는 RATE_LIMIT_PER_MINUTE 환경변수(기본 20/분). 인스턴스별 메모리 저장이라 max-instances 2면
최대 2배까지 허용될 수 있음 — 과금 폭탄 방지 목적엔 충분.
"""
from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import get_settings


def _session_key(request: Request) -> str:
    return str(request.path_params.get("session_id") or get_remote_address(request))


limiter = Limiter(key_func=_session_key)


def chat_rate_limit() -> str:
    return f"{get_settings().rate_limit_per_minute}/minute"


def rate_limit_exceeded_handler(request: Request, exc: Exception) -> JSONResponse:
    # 에러 형식은 FastAPI 기본과 같게 {"detail": ...} (docs/api-spec.md)
    assert isinstance(exc, RateLimitExceeded)
    return JSONResponse(
        status_code=429, content={"detail": "요청이 너무 많아요. 잠시 후 다시 시도해 주세요."}
    )
