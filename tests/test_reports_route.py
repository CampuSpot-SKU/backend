"""본인 신고 조회 라우트 — DB 없이 도는 테스트 (작업 1-12). 실제 조회는 test_reports_api.py(DB 필요)."""
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app


class _NoRowDb:
    """어떤 조회를 해도 결과가 없는 가짜 DB 세션."""

    def execute(self, *_a: Any, **_k: Any) -> Any:
        class _R:
            def first(self) -> None:
                return None

        return _R()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: _NoRowDb()
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def test_unknown_report_is_404(client: TestClient) -> None:
    res = client.get("/api/v1/reports/1", params={"session_id": str(uuid.uuid4())})
    assert res.status_code == 404
    assert res.json() == {"detail": "신고를 찾을 수 없어요."}


def test_bad_parameters_are_422(client: TestClient) -> None:
    sid = str(uuid.uuid4())
    assert client.get("/api/v1/reports/abc", params={"session_id": sid}).status_code == 422
    assert client.get("/api/v1/reports/1", params={"session_id": "not-a-uuid"}).status_code == 422
    assert client.get("/api/v1/reports/1").status_code == 422
