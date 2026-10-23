"""챗봇 레이트 리미팅 (명세서 11장: slowapi로 요청 제한, 작업 S-3).

두 겹으로 센다:
- **세션당** (기본 키 = URL의 session_id): 메시지·새로고침 `RATE_LIMIT_PER_MINUTE`(기본 20/분),
  사진 10/분. 한 대화창이 폭주하는 것을 막는다.
- **접속 IP당** (S-3 추가): 세션을 새로 만들어 세션당 한도를 피하는 것을 막는다.
  ① 세션 만들기 `SESSION_CREATE_LIMIT`(기본 10/분·100/시간)
  ② 메시지·새로고침·사진을 합쳐 `CHAT_IP_LIMIT`(기본 60/분) — 학교 와이파이처럼 여러 학생이
     같은 IP를 쓰는 경우에도 막히지 않도록 세션당 한도의 3배로 넉넉히 둔다.

저장은 인스턴스 메모리라 Cloud Run 인스턴스가 최대 2개면 최악의 경우 2배까지 허용된다 —
과금 폭탄·장난 신고 억제 목적엔 충분하다(공지 #23, 재학생 인증 대신 이 방식으로 결정).
"""
from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from app.config import get_settings


def client_ip(request: Request) -> str:
    """요청을 보낸 사람의 IP.

    Cloud Run은 실제 접속 IP를 X-Forwarded-For의 **맨 끝**에 덧붙인다. 맨 앞 값은 요청자가
    헤더에 아무 값이나 넣어 바꿀 수 있어서(10/23 배포 사이트에서 확인 — 가짜 값을 바꿔 가며
    보내면 로그인 실패 제한이 걸리지 않았음) 끝 값을 쓴다. 헤더가 없으면(테스트 등) 연결 주소.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    if parts:
        return parts[-1]
    return request.client.host if request.client else "unknown"


def _session_key(request: Request) -> str:
    return str(request.path_params.get("session_id") or client_ip(request))


limiter = Limiter(key_func=_session_key)


def chat_rate_limit() -> str:
    return f"{get_settings().rate_limit_per_minute}/minute"


def session_create_limit() -> str:
    return get_settings().session_create_limit


def chat_ip_limit() -> str:
    return get_settings().chat_ip_limit


# 메시지·새로고침·사진 라우트가 함께 쓰는 IP 한도 (scope가 같으면 한 통에서 센다)
chat_ip_shared_limit = limiter.shared_limit(chat_ip_limit, scope="chat-ip", key_func=client_ip)


def rate_limit_exceeded_handler(request: Request, exc: Exception) -> JSONResponse:
    # 에러 형식은 FastAPI 기본과 같게 {"detail": ...} (docs/api-spec.md)
    assert isinstance(exc, RateLimitExceeded)
    return JSONResponse(
        status_code=429,
        content={"detail": "요청이 너무 많아요. 잠시 후 다시 시도해 주세요."},
    )
