"""상세보기 유사 사례 API 테스트 (작업 1-8c, 명세서 3-3·5-1 `/admin/reports/{id}/similar`).

TEST_DATABASE_URL(마이그레이션 끝난 테스트용 빈 DB)이 있을 때만 — 운영 DB 금지!
이 테스트 전용 카테고리를 만들어 그 안에서만 비교하고, 끝나면 지운다. 임베딩은 768차원 단위 벡터를 직접 만든다
(코사인 거리 = 1 − cos θ 라서 각도로 거리를 정할 수 있음).
"""
import math
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL 없음 — DB 통합테스트 생략")
if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB
    os.environ["JWT_SECRET"] = "integration-test-secret-at-least-32-bytes!!"

import bcrypt
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.models import Admin, Category, Report, ReportStatusHistory
from app.models.enums import Priority, ReportStatus
from app.models.knowledge import EMBEDDING_DIM

PASSWORD = "test-only-password"
BASE = "/api/v1/admin/reports"


def vec(distance: float) -> list[float]:
    """기준 벡터(첫 축)와 코사인 거리가 distance인 단위 벡터."""
    cos = 1 - distance
    v = [0.0] * EMBEDDING_DIM
    v[0], v[1] = cos, math.sqrt(max(0.0, 1 - cos * cos))
    return v


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def headers(client: TestClient) -> dict[str, str]:
    db = get_sessionmaker()()
    a = Admin(login_id=f"t_{uuid.uuid4().hex[:8]}",
              password_hash=bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode(), name="테스트관리자")
    db.add(a)
    db.commit()
    login_id = a.login_id
    db.close()
    res = client.post("/api/v1/admin/auth/login", json={"login_id": login_id, "password": PASSWORD})
    assert res.status_code == 200
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@pytest.fixture()
def cases() -> Iterator[dict[str, uuid.UUID]]:
    now = datetime.now(UTC)
    db = get_sessionmaker()()
    cat = Category(name=f"유사테스트-{uuid.uuid4().hex[:6]}", is_active=True)
    other = Category(name=f"유사다른-{uuid.uuid4().hex[:6]}", is_active=True)
    db.add_all([cat, other])
    db.flush()
    ids: dict[str, uuid.UUID] = {}

    def add(name: str, emb: list[float] | None, status: ReportStatus = ReportStatus.RESOLVED,
            category: Category = cat, memos: tuple[str, ...] = ()) -> None:
        r = Report(category_id=category.id, priority=Priority.P3, status=status, description=f"유사 {name}",
                   location_raw="테스트 위치", created_at=now - timedelta(days=3), embedding=emb)
        db.add(r)
        db.flush()
        for n, memo in enumerate(memos):  # 해결 → (재오픈) → 다시 해결
            db.add(ReportStatusHistory(report_id=r.id, from_status=ReportStatus.IN_PROGRESS,
                                       to_status=ReportStatus.RESOLVED, memo=memo,
                                       changed_at=now - timedelta(days=2) + timedelta(hours=n)))
        ids[name] = r.id

    add("target", vec(0), status=ReportStatus.RECEIVED)
    add("near", vec(0.02), memos=("배관 교체", "배관 재교체 완료"))
    add("mid", vec(0.2), status=ReportStatus.CLOSED)
    add("far", vec(0.6))                                   # 기준보다 멀어서 빠짐
    add("open", vec(0.01), status=ReportStatus.IN_PROGRESS)  # 아직 처리 중 → 빠짐
    add("other_cat", vec(0.01), category=other)             # 다른 카테고리 → 빠짐
    add("no_emb", None)                                     # 임베딩 없음 → 빠짐
    add("new", None, status=ReportStatus.RECEIVED)          # 조회 대상인데 임베딩 아직 없음
    db.commit()
    cat_ids = (cat.id, other.id)
    db.close()
    yield ids
    db = get_sessionmaker()()
    db.execute(delete(Report).where(Report.id.in_(ids.values())))
    db.execute(delete(Category).where(Category.id.in_(cat_ids)))
    db.commit()
    db.close()


def test_requires_token(client: TestClient) -> None:
    assert client.get(f"{BASE}/{uuid.uuid4()}/similar").status_code == 401


def test_similar_order_filter_and_memo(client: TestClient, headers: dict[str, str],
                                       cases: dict[str, uuid.UUID]) -> None:
    res = client.get(f"{BASE}/{cases['target']}/similar", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["embedding_ready"] is True
    assert [i["description"] for i in body["items"]] == ["유사 near", "유사 mid"]
    near = body["items"][0]
    assert set(near) == {"id", "display_no", "category", "building", "floor", "detail", "location_raw",
                         "description", "status", "created_at", "resolved_at", "resolution_memo", "distance"}
    assert near["resolution_memo"] == "배관 재교체 완료"  # 마지막 해결 때 메모
    assert near["distance"] == pytest.approx(0.02, abs=1e-3)
    assert body["items"][1]["resolution_memo"] is None and body["items"][1]["status"] == "종료"

    one = client.get(f"{BASE}/{cases['target']}/similar", params={"limit": 1}, headers=headers).json()
    assert [i["description"] for i in one["items"]] == ["유사 near"]


def test_not_ready_missing_and_validation(client: TestClient, headers: dict[str, str],
                                          cases: dict[str, uuid.UUID]) -> None:
    res = client.get(f"{BASE}/{cases['new']}/similar", headers=headers)
    assert res.status_code == 200 and res.json() == {"embedding_ready": False, "items": []}
    assert client.get(f"{BASE}/{uuid.uuid4()}/similar", headers=headers).status_code == 404
    for bad in (0, 11):
        assert client.get(f"{BASE}/{cases['target']}/similar", params={"limit": bad},
                          headers=headers).status_code == 422
