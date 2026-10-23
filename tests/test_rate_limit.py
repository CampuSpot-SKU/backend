"""학생 챗봇 IP당 레이트 리밋 — DB 없이 도는 테스트 (작업 S-3, 공지 #23).

세션당 한도(20/분)는 세션을 새로 만들면 피할 수 있어서, 접속 IP 기준 한도를 두 겹 더 둠:
세션 만들기 10/분·100/시간, 메시지·새로고침·사진 합계 60/분.
"""
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app
from app.rate_limit import client_ip


class _FakeDb:
    """세션 생성용: add된 객체에 id만 채워 줌. get은 항상 None(세션 없음 → 404)."""

    def add(self, obj: Any) -> None:
        obj.id = uuid.uuid4()

    def commit(self) -> None:
        pass

    def refresh(self, _obj: Any) -> None:
        pass

    def get(self, *_args: Any) -> None:
        return None


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: _FakeDb()
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def _new_session(c: TestClient, ip: str) -> Any:
    return c.post("/api/v1/chat/sessions", headers={"X-Forwarded-For": ip})


def test_session_create_limited_per_ip(client: TestClient) -> None:
    for _ in range(10):
        assert _new_session(client, "1.1.1.1").status_code == 201
    res = _new_session(client, "1.1.1.1")
    assert res.status_code == 429
    assert "잠시 후" in res.json()["detail"]
    assert _new_session(client, "2.2.2.2").status_code == 201  # 다른 IP는 영향 없음


def test_spoofed_first_forwarded_value_does_not_bypass(client: TestClient) -> None:
    for i in range(10):
        assert _new_session(client, f"10.0.0.{i}, 1.1.1.1").status_code == 201
    assert _new_session(client, "10.0.0.200, 1.1.1.1").status_code == 429


def test_chat_ip_limit_shared_across_sessions_and_routes(client: TestClient) -> None:
    """세션을 바꿔 가며 보내도 같은 IP면 메시지·새로고침·사진을 합쳐 60/분에서 막힘."""
    h = {"X-Forwarded-For": "3.3.3.3"}
    for i in range(60):
        sid = uuid.uuid4()  # 매번 다른 세션 → 세션당 한도(20/분)에는 안 걸림
        url = (f"/api/v1/chat/sessions/{sid}/reset" if i % 2 == 0
               else f"/api/v1/chat/sessions/{sid}/photo")
        res = client.post(url, headers=h) if i % 2 == 0 else client.delete(url, headers=h)
        assert res.status_code == 404  # 세션이 없을 뿐, 한도 안
    res = client.post(f"/api/v1/chat/sessions/{uuid.uuid4()}/reset", headers=h)
    assert res.status_code == 429
    other = client.post(f"/api/v1/chat/sessions/{uuid.uuid4()}/reset",
                        headers={"X-Forwarded-For": "4.4.4.4"})
    assert other.status_code == 404


def test_session_limit_still_applies(client: TestClient) -> None:
    """기존 세션당 한도(20/분)는 그대로 — IP 한도(60)보다 먼저 걸림."""
    sid = uuid.uuid4()
    h = {"X-Forwarded-For": "5.5.5.5"}
    for _ in range(20):
        assert client.post(f"/api/v1/chat/sessions/{sid}/reset", headers=h).status_code == 404
    assert client.post(f"/api/v1/chat/sessions/{sid}/reset", headers=h).status_code == 429


class _Req:
    def __init__(self, xff: str | None, host: str = "7.7.7.7") -> None:
        self.headers = {"x-forwarded-for": xff} if xff is not None else {}
        self.client = type("C", (), {"host": host})()


@pytest.mark.parametrize(("xff", "expected"), [
    ("1.2.3.4", "1.2.3.4"),
    ("6.6.6.6, 1.2.3.4", "1.2.3.4"),
    ("6.6.6.6,1.2.3.4 ", "1.2.3.4"),
    ("", "7.7.7.7"),
    (None, "7.7.7.7"),
])
def test_client_ip_uses_last_forwarded_value(xff: str | None, expected: str) -> None:
    assert client_ip(_Req(xff)) == expected  # type: ignore[arg-type]
