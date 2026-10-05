"""관리자 통계 API 테스트 (작업 1-16, 명세서 4-2).

- 계산 기준(SLA 준수/위반/제외, 처리시간) 단위테스트: DB 없이 항상 실행
- /admin/stats 통합테스트: TEST_DATABASE_URL(마이그레이션 끝난 테스트용 빈 DB)이 있을 때만 — 운영 DB 금지!
  다른 테스트가 같은 DB에 신고를 넣으므로, 이 테스트 전용 카테고리를 만들어 그 카테고리 줄만 정확히 비교하고
  끝나면 지운다(같은 DB로 두 번 돌려도 통과).
"""
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
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
from app.services.stats_query import _group, _Row, sla_outcome

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)


def row(status: ReportStatus, created_h_ago: float, deadline_h: float | None,
        resolved_h_after: float | None = None, priority: Priority = Priority.P3) -> _Row:
    created = NOW - created_h_ago * H
    return _Row(
        category_id=None,
        priority=priority,
        status=status,
        created_at=created,
        sla_deadline=created + deadline_h * H if deadline_h is not None else None,
        resolved_at=created + resolved_h_after * H if resolved_h_after is not None else None,
    )


# ── 단위테스트: 계산 기준 ──

def test_sla_outcome_rules() -> None:
    R, C, O = ReportStatus.RESOLVED, ReportStatus.CLOSED, ReportStatus.IN_PROGRESS
    assert sla_outcome(row(R, 10, 4, resolved_h_after=3), NOW) is True       # 마감 안에 해결
    assert sla_outcome(row(C, 10, 4, resolved_h_after=4), NOW) is True       # 마감 시각 정각 = 준수
    assert sla_outcome(row(R, 10, 4, resolved_h_after=5), NOW) is False      # 늦게 해결
    assert sla_outcome(row(O, 10, 4), NOW) is False                          # 처리 중인데 마감 지남
    assert sla_outcome(row(ReportStatus.RECEIVED, 1, 4), NOW) is None        # 아직 마감 전 → 제외
    assert sla_outcome(row(R, 10, None, resolved_h_after=1), NOW) is None    # 마감 없음 → 제외
    assert sla_outcome(row(R, 10, 4), NOW) is None                           # 해결 이력 없음 → 제외


def test_group_avg_and_rate() -> None:
    rows = [
        row(ReportStatus.RESOLVED, 50, 24, resolved_h_after=2),   # 준수, 2h
        row(ReportStatus.CLOSED, 50, 24, resolved_h_after=30),    # 위반, 30h
        row(ReportStatus.ASSIGNED, 30, 24),                       # 위반(처리 중 초과)
        row(ReportStatus.RECEIVED, 1, 24),                        # 제외
    ]
    g = _group(rows, NOW)
    assert (g.count, g.resolved_count, g.avg_resolution_hours) == (4, 2, 16.0)
    assert (g.sla.met, g.sla.breached, g.sla.compliance_pct) == (1, 2, 33.3)


def test_group_empty() -> None:
    g = _group([], NOW)
    assert g.count == 0 and g.avg_resolution_hours is None and g.sla.compliance_pct is None


# ── 통합테스트: /admin/stats ──

needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL 없음 — DB 통합테스트 생략")

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


@pytest.fixture()
def stats_category() -> Iterator[uuid.UUID]:
    """이 테스트 전용 카테고리 + 계산 결과를 알고 있는 신고 5건. 끝나면 지움."""
    now = datetime.now(UTC)
    db = get_sessionmaker()()
    cat = Category(name=f"통계테스트-{uuid.uuid4().hex[:6]}", is_active=True)
    db.add(cat)
    db.flush()

    def add(status: ReportStatus, created_h_ago: float, deadline_h: float,
            resolved: tuple[float, ...] = (), priority: Priority = Priority.P2) -> None:
        created = now - created_h_ago * H
        r = Report(category_id=cat.id, priority=priority, status=status, description="테스트 신고(통계)",
                   location_raw="테스트 위치", created_at=created, sla_deadline=created + deadline_h * H)
        db.add(r)
        db.flush()
        for after in resolved:  # 해결 전이 시각들 (재오픈 후 다시 해결이면 여러 개)
            db.add(ReportStatusHistory(report_id=r.id, from_status=ReportStatus.IN_PROGRESS,
                                       to_status=ReportStatus.RESOLVED, changed_at=created + after * H))

    add(ReportStatus.RESOLVED, 48, 24, resolved=(4,))          # 준수, 4h
    add(ReportStatus.CLOSED, 48, 24, resolved=(2, 30))        # 재오픈 후 30h에 다시 해결 → 위반, 30h
    add(ReportStatus.IN_PROGRESS, 30, 24)                     # 처리 중 초과 → 위반
    add(ReportStatus.RECEIVED, 1, 24, priority=Priority.P1)   # 마감 전 → 제외
    add(ReportStatus.RESOLVED, 24 * 40, 24, resolved=(1,))     # 40일 전 — days=30이면 빠짐
    db.commit()
    cid = cat.id
    db.close()
    yield cid

    db = get_sessionmaker()()
    db.execute(delete(Report).where(Report.category_id == cid))  # 이력은 CASCADE
    db.execute(delete(Category).where(Category.id == cid))
    db.commit()
    db.close()


@needs_db
def test_requires_token(client: TestClient) -> None:
    assert client.get(f"{BASE}/stats").status_code == 401


@needs_db
def test_stats_shape_and_numbers(client: TestClient, headers: dict[str, str], stats_category: uuid.UUID) -> None:
    res = client.get(f"{BASE}/stats", params={"days": 30}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"period", "total", "by_status", "by_category", "by_priority"}
    assert body["period"]["days"] == 30 and body["period"]["since"] is not None
    assert [s["status"] for s in body["by_status"]] == ["접수", "배정", "처리중", "해결", "종료"]
    assert [p["priority"] for p in body["by_priority"]] == ["P1", "P2", "P3", "P4"]

    mine = next(c for c in body["by_category"] if c["category"]["id"] == str(stats_category))
    assert mine["count"] == 4  # 40일 전 1건 제외
    assert mine["resolved_count"] == 2
    assert mine["avg_resolution_hours"] == 17.0  # (4 + 30) / 2 — 마지막 해결 시각 기준
    assert mine["sla"] == {"met": 1, "breached": 2, "compliance_pct": 33.3}
    assert set(mine) == {"category", "is_active", "count", "resolved_count", "avg_resolution_hours", "sla"}

    counts = [c["count"] for c in body["by_category"]]
    assert counts == sorted(counts, reverse=True)  # 건수 많은 순
    assert body["total"]["count"] == sum(s["count"] for s in body["by_status"])
    assert body["total"]["count"] == sum(p["count"] for p in body["by_priority"])


@needs_db
def test_stats_all_period_and_validation(client: TestClient, headers: dict[str, str],
                                         stats_category: uuid.UUID) -> None:
    res = client.get(f"{BASE}/stats", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["period"]["days"] is None and body["period"]["since"] is None
    mine = next(c for c in body["by_category"] if c["category"]["id"] == str(stats_category))
    assert mine["count"] == 5 and mine["sla"]["met"] == 2  # 40일 전 건(1h 해결)까지 포함

    for bad in (0, 366, "abc"):
        assert client.get(f"{BASE}/stats", params={"days": bad}, headers=headers).status_code == 422
