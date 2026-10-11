"""Sentry 연결 테스트 (S-3·1-15) — 실제 Sentry로 보내지 않고 가짜 전송으로 이벤트만 확인."""
from typing import Any

import httpx
import sentry_sdk

from app.monitoring import report_ai_failure


class _Box(sentry_sdk.transport.Transport):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Any) -> None:
        for item in envelope.items:
            if item.type == "event":
                self.events.append(item.payload.json)


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://ai.example/api/v1/intent/classify")
    return httpx.HTTPStatusError("err", request=req, response=httpx.Response(code, request=req))


def test_reports_only_failures_ai_did_not_report() -> None:
    box = _Box()
    sentry_sdk.init(dsn="https://k@o0.ingest.sentry.io/0", transport=box, include_local_variables=False)
    try:
        for err in (_status_error(503), httpx.ConnectTimeout("timeout"), _status_error(500)):
            try:
                raise err
            except Exception as e:  # noqa: BLE001
                report_ai_failure("intent", e)
        sentry_sdk.flush()
    finally:
        sentry_sdk.init(dsn=None)
    # 503은 ai 서비스가 이미 남김 → 시간 초과·500 두 건만
    assert len(box.events) == 2
    assert {e["tags"]["ai_failure"] for e in box.events} == {"intent"}
    frames = [f for e in box.events for v in e["exception"]["values"] for f in v["stacktrace"]["frames"]]
    assert all("vars" not in f for f in frames)
