"""관리자 로그인 실패 횟수 제한 — 비밀번호 대입 공격 막기 (작업 S-3).

- 같은 (접속 IP, login_id)에서 로그인에 **실패**한 횟수만 센다. 성공한 로그인은 세지 않으므로
  정상 사용자·테스트는 막히지 않는다.
- 최근 1분 안에 5번 실패하면 그 뒤 시도는 비밀번호를 확인하지 않고 429로 거절(Retry-After 60초).
  시간이 지나면 자동으로 풀린다(이동 창 방식).
- 저장은 인스턴스 메모리(slowapi가 쓰는 limits 라이브러리) — Cloud Run 인스턴스가 최대 2개라
  최악의 경우 1분에 10번까지 허용되지만, bcrypt(rounds 12)와 합치면 대입 공격엔 충분히 느리다.
- IP는 Cloud Run이 X-Forwarded-For 맨 끝에 덧붙이는 실제 접속 IP(`app/rate_limit.client_ip`).
  처음엔 첫 값을 썼는데 요청자가 바꿀 수 있어 우회됐음(10/23 수정). 학생 챗봇 레이트 리밋
  (app/rate_limit.py)과 달리 로그인엔 세션 ID가 없어서 IP를 쓴다. login_id를 키에 같이 넣어
  같은 IP(학교 와이파이 등)의 다른 관리자는 서로 영향을 받지 않게 한다.
"""
from fastapi import Request
from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter

from app.rate_limit import client_ip

LOGIN_FAILURE_LIMIT = parse("5/minute")
RETRY_AFTER_SECONDS = 60

_storage = MemoryStorage()
_limiter = MovingWindowRateLimiter(_storage)


def _key(request: Request, login_id: str, scope: str) -> tuple[str, str]:
    return (scope, f"{client_ip(request)}|{login_id.lower()}")


def is_blocked(request: Request, login_id: str, scope: str = "admin-login") -> bool:
    """이미 1분 안에 5번 실패했으면 True (이번 시도는 세지 않음). scope: 관리자 "admin-login" / 학생 "user-login"(1-24)."""
    return not _limiter.test(LOGIN_FAILURE_LIMIT, *_key(request, login_id, scope))


def record_failure(request: Request, login_id: str, scope: str = "admin-login") -> None:
    _limiter.hit(LOGIN_FAILURE_LIMIT, *_key(request, login_id, scope))


def reset_all() -> None:
    """테스트용 — 기록 전부 지우기."""
    _storage.reset()
