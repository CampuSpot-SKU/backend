"""관리자 로그인 실패 횟수 제한 — DB 없이 도는 테스트 (작업 S-3)."""
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app
from app.routers import admin as admin_router
from app.services import login_guard

URL = "/api/v1/admin/auth/login"
RIGHT = "right-password"


def _fake_authenticate(_db: Any, login_id: str, password: str) -> Any:
    return SimpleNamespace(id=uuid.uuid4(), login_id=login_id) if password == RIGHT else None


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    login_guard.reset_all()
    monkeypatch.setattr(admin_router, "authenticate", _fake_authenticate)
    monkeypatch.setattr(admin_router, "create_access_token", lambda _id: "tok")
    app.dependency_overrides[get_db] = lambda: object()
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    login_guard.reset_all()


def _login(c: TestClient, login_id: str, password: str, ip: str = "1.2.3.4") -> Any:
    return c.post(URL, json={"login_id": login_id, "password": password},
                  headers={"X-Forwarded-For": ip})


def test_blocks_after_five_failures(client: TestClient) -> None:
    for _ in range(5):
        assert _login(client, "admin1", "wrong").status_code == 401
    res = _login(client, "admin1", "wrong")
    assert res.status_code == 429
    assert res.headers["Retry-After"] == "60"
    assert "1분 뒤" in res.json()["detail"]
    # 막힌 동안엔 맞는 비밀번호도 확인하지 않음
    assert _login(client, "admin1", RIGHT).status_code == 429


def test_success_is_not_counted(client: TestClient) -> None:
    for _ in range(10):
        assert _login(client, "admin1", RIGHT).status_code == 200
    for _ in range(4):
        assert _login(client, "admin1", "wrong").status_code == 401
    assert _login(client, "admin1", RIGHT).status_code == 200


def test_other_account_or_ip_not_affected(client: TestClient) -> None:
    for _ in range(5):
        _login(client, "admin1", "wrong")
    assert _login(client, "admin1", "wrong").status_code == 429
    assert _login(client, "admin2", RIGHT).status_code == 200  # 같은 IP, 다른 아이디
    assert _login(client, "admin1", RIGHT, ip="5.6.7.8").status_code == 200  # 다른 IP
    assert _login(client, "ADMIN1", "wrong").status_code == 429  # 대소문자만 바꿔 우회 불가


def test_forwarded_for_last_value_is_used(client: TestClient) -> None:
    """Cloud Run이 맨 끝에 붙이는 실제 IP 기준 — 맨 앞(요청자가 넣는 값)을 바꿔도 우회 안 됨."""
    for i in range(5):
        _login(client, "admin1", "wrong", ip=f"10.0.0.{i}, 9.9.9.9")
    assert _login(client, "admin1", "wrong", ip="10.0.0.99, 9.9.9.9").status_code == 429
    assert _login(client, "admin1", "wrong", ip="9.9.9.9").status_code == 429
    assert _login(client, "admin1", "wrong", ip="9.9.9.9, 8.8.8.8").status_code == 401
