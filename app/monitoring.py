"""에러 트래킹(Sentry) — 처리 못 한 오류 + ai 서비스 호출 실패 알림 (S-3·1-15, 명세 6장).

- SENTRY_DSN이 비어 있으면 아무것도 하지 않음(로컬·테스트).
- 개인정보: send_default_pii=False(요청 본문·IP를 안 보냄), include_local_variables=False(스택의 지역 변수를 안 보냄).
- ai 서비스가 Gemini 실패로 503을 돌려준 경우는 ai 쪽이 이미 Sentry에 남기므로 여기선 보내지 않는다.
  ai 서비스에 닿지 못한 경우(연결 실패·시간 초과·503 외 오류·응답 형식 오류)만 `ai_failure=<기능>` 태그로 보낸다.
"""
import httpx
import sentry_sdk

from app.config import get_settings


def init_sentry() -> None:
    dsn = get_settings().sentry_dsn
    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        send_default_pii=False,
        include_local_variables=False,
        traces_sample_rate=0.2,
    )


def report_ai_failure(feature: str, error: BaseException) -> None:
    """ai 서비스 호출 실패를 Sentry로 (ai가 이미 남긴 503은 빼고)."""
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 503:
        return
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("ai_failure", feature)
        sentry_sdk.capture_exception(error)
