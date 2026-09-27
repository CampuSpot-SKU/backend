"""요청별 correlation ID 부여.

- 요청 헤더에 X-Request-ID가 있으면 그대로 쓰고, 없으면 새로 만든다.
- 응답 헤더 X-Request-ID로 돌려줌 → 프론트/사용자 제보에서 이 값으로 로그 검색 가능.
- 로그 한 줄마다 [request_id]가 찍히도록 logging 필터에 연결.
  (Cloud Run 로그 뷰어에서 이 ID로 검색하면 한 요청의 흐름을 전부 볼 수 있음)
- SSE 스트리밍 응답과도 충돌하지 않도록 BaseHTTPMiddleware 대신 순수 ASGI로 구현.
"""
import logging
import uuid
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER = "x-request-id"
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        incoming = headers.get(HEADER.encode(), b"").decode()[:64]
        rid = incoming or uuid.uuid4().hex
        token = request_id_ctx.set(rid)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((HEADER.encode(), rid.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_ctx.reset(token)


class RequestIdLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdLogFilter())
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
