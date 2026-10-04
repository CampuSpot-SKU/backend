"""탐지·예측 관리자 API 통합테스트 — 문제 후보 목록·승격/기각, 재발 예측 목록 (작업 1-8).

TEST_DATABASE_URL(마이그레이션 끝난 테스트용 빈 DB)이 있을 때만 실행 — 운영 DB 금지!
배치(ai)가 채울 problem_clusters·prediction_stats 행은 테스트가 직접 넣는다.
응답 모양은 frontend 계약(team-docs briefs/1-8.md)과 같아야 함: building·category는 {id, name}.
"""
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import bcrypt
import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL 없음 — DB 통합테스트 생략")

if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB
    os.environ["JWT_SECRET"] = "integration-test-secret-at-least-32-bytes!!"

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.models import Admin, Building, Category, PredictionStat, ProblemCluster, Report
from app.models.enums import ClusterStatus, Priority, ReportStatus
from app.models.problem import problem_cluster_reports

PASSWORD = "test-only-password"
BASE = "/api/v1/admin"


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
    a = Admin(
        login_id=f"t_{uuid.uuid4().hex[:8]}",
        password_hash=bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode(),
        name="테스트관리자",
    )
    db.add(a)
    db.commit()
    login_id = a.login_id
    db.close()
    res = client.post(f"{BASE}/auth/login", json={"login_id": login_id, "password": PASSWORD})
    assert res.status_code == 200
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _refs() -> tuple[Building, Category]:
    db = get_sessionmaker()()
    building = db.scalar(select(Building).order_by(Building.name).limit(1))
    category = db.scalar(select(Category).where(Category.name == "시설·설비"))
    db.close()
    assert building is not None and category is not None  # 시드(건물 12개·카테고리)가 있어야 함
    return building, category


def make_cluster(detected_ago_h: float, n_reports: int, with_building: bool = True,
                 status: ClusterStatus = ClusterStatus.CANDIDATE) -> uuid.UUID:
    """배치 대신 후보 한 건 + 묶인 신고 n건을 넣음."""
    building, category = _refs()
    db = get_sessionmaker()()
    c = ProblemCluster(
        building_id=building.id if with_building else None,
        detail="2층 화장실" if with_building else None,
        category_id=category.id,
        detected_at=datetime.now(UTC) - timedelta(hours=detected_ago_h),
        status=status,
    )
    db.add(c)
    db.flush()
    for _ in range(n_reports):
        r = Report(category_id=category.id, priority=Priority.P3, status=ReportStatus.RECEIVED,
                   description="테스트 신고(탐지)", location_raw="테스트 위치")
        db.add(r)
        db.flush()
        db.execute(insert(problem_cluster_reports).values(cluster_id=c.id, report_id=r.id))
    db.commit()
    cid = c.id
    db.close()
    return cid


def test_endpoints_require_token(client: TestClient) -> None:
    assert client.get(f"{BASE}/problem-clusters").status_code == 401
    assert client.patch(f"{BASE}/problem-clusters/{uuid.uuid4()}", json={"status": "승격"}).status_code == 401
    assert client.get(f"{BASE}/predictions").status_code == 401


def test_cluster_list_shape_filter_and_order(client: TestClient, headers: dict[str, str]) -> None:
    building, category = _refs()
    older = make_cluster(detected_ago_h=5, n_reports=3)
    newer = make_cluster(detected_ago_h=1, n_reports=4, with_building=False)
    decided = make_cluster(detected_ago_h=2, n_reports=3, status=ClusterStatus.REJECTED)

    res = client.get(f"{BASE}/problem-clusters", params={"status": "후보"}, headers=headers)
    assert res.status_code == 200
    items = res.json()["items"]
    assert all(i["status"] == "후보" for i in items)
    ids = [i["id"] for i in items]
    assert str(decided) not in ids
    assert ids.index(str(newer)) < ids.index(str(older))  # 감지 시각 최신순

    by_id = {i["id"]: i for i in items}
    o = by_id[str(older)]
    assert set(o) == {"id", "building", "detail", "category", "report_count", "detected_at", "status"}
    assert o["building"] == {"id": str(building.id), "name": building.name}
    assert o["category"] == {"id": str(category.id), "name": "시설·설비"}
    assert o["detail"] == "2층 화장실" and o["report_count"] == 3
    datetime.fromisoformat(o["detected_at"])  # ISO 문자열
    n = by_id[str(newer)]
    assert n["building"] is None and n["detail"] is None and n["report_count"] == 4

    # status 없으면 전체 (기각 포함)
    all_ids = [i["id"] for i in client.get(f"{BASE}/problem-clusters", headers=headers).json()["items"]]
    assert {str(older), str(newer), str(decided)} <= set(all_ids)
    # 잘못된 status 값은 422
    assert client.get(f"{BASE}/problem-clusters", params={"status": "없음"}, headers=headers).status_code == 422


def test_cluster_promote_reject_once(client: TestClient, headers: dict[str, str]) -> None:
    a = make_cluster(detected_ago_h=1, n_reports=3)
    b = make_cluster(detected_ago_h=1, n_reports=3)

    res = client.patch(f"{BASE}/problem-clusters/{a}", json={"status": "승격"}, headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == str(a) and body["status"] == "승격" and body["report_count"] == 3
    assert body["category"]["name"] == "시설·설비"  # 목록 원소와 같은 모양

    # 한 번 결정되면 다시 못 바꿈
    again = client.patch(f"{BASE}/problem-clusters/{a}", json={"status": "기각"}, headers=headers)
    assert again.status_code == 409
    assert client.patch(f"{BASE}/problem-clusters/{a}", json={"status": "승격"}, headers=headers).status_code == 409

    assert client.patch(f"{BASE}/problem-clusters/{b}", json={"status": "기각"}, headers=headers).json()["status"] == "기각"
    # 승격 목록에 a, 후보 목록에는 a·b 모두 없음
    promoted = [i["id"] for i in client.get(f"{BASE}/problem-clusters", params={"status": "승격"}, headers=headers).json()["items"]]
    candidates = [i["id"] for i in client.get(f"{BASE}/problem-clusters", params={"status": "후보"}, headers=headers).json()["items"]]
    assert str(a) in promoted and str(a) not in candidates and str(b) not in candidates


def test_cluster_patch_bad_input(client: TestClient, headers: dict[str, str]) -> None:
    c = make_cluster(detected_ago_h=1, n_reports=3)
    # 후보로 되돌리기·엉뚱한 값은 422
    assert client.patch(f"{BASE}/problem-clusters/{c}", json={"status": "후보"}, headers=headers).status_code == 422
    assert client.patch(f"{BASE}/problem-clusters/{c}", json={"status": "해결"}, headers=headers).status_code == 422
    assert client.patch(f"{BASE}/problem-clusters/{uuid.uuid4()}", json={"status": "승격"}, headers=headers).status_code == 404
    assert client.patch(f"{BASE}/problem-clusters/not-a-uuid", json={"status": "승격"}, headers=headers).status_code == 422


def test_predictions_only_computed_rows_sorted(client: TestClient, headers: dict[str, str]) -> None:
    building, category = _refs()
    now = datetime.now(UTC)
    tag = uuid.uuid4().hex[:6]  # 같은 테스트 DB에서 여러 번 돌려도 이번 실행 행만 찾게
    db = get_sessionmaker()()
    later = PredictionStat(building_id=building.id, detail=f"정수기-{tag}", category_id=category.id,
                           avg_recurrence_days=14.25, last_occurred_at=now - timedelta(days=3),
                           predicted_next_at=now + timedelta(days=11))
    sooner = PredictionStat(building_id=None, detail=None, category_id=category.id,
                            avg_recurrence_days=7.0 + int(tag, 16) / 1e9, last_occurred_at=now - timedelta(days=9),
                            predicted_next_at=now - timedelta(days=2))
    pending = PredictionStat(building_id=building.id, detail=f"계산 전-{tag}", category_id=category.id)
    db.add_all([later, sooner, pending])
    db.commit()
    sooner_days = sooner.avg_recurrence_days
    db.close()

    res = client.get(f"{BASE}/predictions", headers=headers)
    assert res.status_code == 200
    items = res.json()["items"]
    assert all(i["detail"] != f"계산 전-{tag}" for i in items)  # 평균·예상 시점 없는 행은 안 나옴
    for i in items:
        assert set(i) == {"building", "detail", "category", "avg_recurrence_days", "predicted_next_at"}
        assert i["avg_recurrence_days"] is not None and i["predicted_next_at"] is not None
    times = [datetime.fromisoformat(i["predicted_next_at"]) for i in items]
    assert times == sorted(times)  # 예상 시점 빠른 순

    mine = [i for i in items if i["detail"] == f"정수기-{tag}"]
    assert len(mine) == 1 and mine[0]["avg_recurrence_days"] == 14.25
    assert mine[0]["building"] == {"id": str(building.id), "name": building.name}
    assert mine[0]["category"] == {"id": str(category.id), "name": "시설·설비"}
    assert any(i["building"] is None and i["avg_recurrence_days"] == sooner_days for i in items)
