"""관리자 기능 유닛테스트 — SLA 상태 계산, JWT, 비밀번호, 상태 변경 (DB 없이 실행, 작업 1-6)."""
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import pytest

from app.config import get_settings
from app.models.enums import ReportStatus
from app.services import auth
from app.services.report_query import sla_status_of
from app.services.report_workflow import InvalidTransitionError, check_transition

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
DEADLINE = T0 + timedelta(hours=24)  # P2 = 24시간


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (T0 + timedelta(hours=1), "온타임"),
        (T0 + timedelta(hours=12), "임박"),  # 절반 경과 시점부터 임박
        (T0 + timedelta(hours=23), "임박"),
        (T0 + timedelta(hours=24), "초과"),
        (T0 + timedelta(hours=30), "초과"),
    ],
)
def test_sla_status_open_report(now: datetime, expected: str) -> None:
    assert sla_status_of(ReportStatus.IN_PROGRESS, T0, DEADLINE, now) == expected


def test_sla_status_none_for_closed_or_no_deadline() -> None:
    late = T0 + timedelta(hours=30)
    assert sla_status_of(ReportStatus.RESOLVED, T0, DEADLINE, late) is None
    assert sla_status_of(ReportStatus.CLOSED, T0, DEADLINE, late) is None
    assert sla_status_of(ReportStatus.RECEIVED, T0, None, late) is None


@pytest.fixture
def jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "unit-test-secret-at-least-32-bytes-long!!")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_token_roundtrip(jwt_secret: None) -> None:
    admin_id = uuid.uuid4()
    assert auth.decode_access_token(auth.create_access_token(admin_id)) == admin_id


def test_token_expired_or_tampered(jwt_secret: None) -> None:
    old = auth.create_access_token(uuid.uuid4(), now=datetime.now(UTC) - timedelta(days=1))
    assert auth.decode_access_token(old) is None
    token = auth.create_access_token(uuid.uuid4())
    assert auth.decode_access_token(token[:-2] + "xx") is None
    assert auth.decode_access_token("not-a-token") is None


def test_no_secret_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "")
    get_settings.cache_clear()
    with pytest.raises(auth.AuthNotConfiguredError):
        auth.create_access_token(uuid.uuid4())
    get_settings.cache_clear()


def test_verify_password() -> None:
    h = bcrypt.hashpw(b"right-password", bcrypt.gensalt(4)).decode()
    assert auth.verify_password("right-password", h)
    assert not auth.verify_password("wrong-password", h)
    assert not auth.verify_password("right-password", None)  # 없는 아이디
    assert not auth.verify_password("x" * 100, h)  # bcrypt 72바이트 초과


def test_same_status_change_rejected() -> None:
    with pytest.raises(InvalidTransitionError):
        check_transition(ReportStatus.RECEIVED, ReportStatus.RECEIVED)
    check_transition(ReportStatus.RECEIVED, ReportStatus.ASSIGNED)  # 예외 없음
