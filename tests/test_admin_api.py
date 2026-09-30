"""관리자 API 통합테스트 — 로그인 → 목록(필터·SLA) → 상세 → 상태 변경 (작업 1-6).

TEST_DATABASE_URL(마이그레이션 끝난 테스트용 빈 DB)이 있을 때만 실행 — 운영 DB 금지!
관리자 계정은 테스트가 직접 만든다 (실제 관리자 비밀번호는 공개 레포에 넣지 않음).
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
from sqlalchemy import select

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.models import Admin, Category, Report, ReportStatusHistory
from app.models.enums import Priority, ReportStatus

PASSWORD = "test-only-password"


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client: TestClient) -> Admin:
    db = get_sessionmaker()()
    a = Admin(
        login_id=f"t_{uuid.uuid4().hex[:8]}",
        password_hash=bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode(),
        name="테스트관리자",
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    db.close()
    return a


@pytest.fixture(scope="module")
def headers(client: TestClient, admin: Admin) -> dict[str, str]:
    res = client.post("/api/v1/admin/auth/login", json={"login_id": admin.login_id, "password": PASSWORD})
    assert res.status_code == 200
    assert res.json()["token_type"] == "bearer"
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def make_report(created_ago_h: float, sla_h: float, status: ReportStatus = ReportStatus.RECEIVED,
                priority: Priority = Priority.P2, category: str = "시설·설비") -> Report:
    """시각을 직접 지정해서 신고를 넣음 (SLA 상태 검증용)."""
    db = get_sessionmaker()()
    cat = db.scalar(select(Category).where(Category.name == category))
    assert cat is not None
    created = datetime.now(UTC) - timedelta(hours=created_ago_h)
    r = Report(category_id=cat.id, priority=priority, status=status, description="테스트 신고",
               location_raw="3동 2층 화장실", created_at=created,
               sla_deadline=created + timedelta(hours=sla_h))
    db.add(r)
    db.flush()
    db.add(ReportStatusHistory(report_id=r.id, from_status=None, to_status=ReportStatus.RECEIVED))
    db.commit()
    db.refresh(r)
    db.close()
    return r


def test_login_wrong_password(client: TestClient, admin: Admin) -> None:
    res = client.post("/api/v1/admin/auth/login", json={"login_id": admin.login_id, "password": "nope"})
    assert res.status_code == 401
    res = client.post("/api/v1/admin/auth/login", json={"login_id": "no-such-admin", "password": "nope"})
    assert res.status_code == 401


def test_admin_endpoints_require_token(client: TestClient) -> None:
    assert client.get("/api/v1/admin/reports").status_code == 401
    bad = {"Authorization": "Bearer forged.token.value"}
    assert client.get("/api/v1/admin/reports", headers=bad).status_code == 401


def test_list_filters_and_sla(client: TestClient, headers: dict[str, str]) -> None:
    ontime = make_report(created_ago_h=1, sla_h=24)
    soon = make_report(created_ago_h=13, sla_h=24)
    late = make_report(created_ago_h=30, sla_h=24, priority=Priority.P1)
    done = make_report(created_ago_h=30, sla_h=24, status=ReportStatus.RESOLVED)

    res = client.get("/api/v1/admin/reports", params={"limit": 200}, headers=headers)
    assert res.status_code == 200
    by_no = {i["display_no"]: i for i in res.json()["items"]}
    assert by_no[ontime.display_no]["sla_status"] == "온타임"
    assert by_no[soon.display_no]["sla_status"] == "임박"
    assert by_no[late.display_no]["sla_status"] == "초과"
    assert by_no[done.display_no]["sla_status"] is None
    item = by_no[ontime.display_no]
    assert item["category"]["name"] == "시설·설비" and item["building"] is None
    assert item["location_raw"] == "3동 2층 화장실"

    # SQL 필터와 파이썬 계산이 같은 기준인지: 필터 결과는 전부 그 sla_status여야 함
    for sla, expected_no in (("온타임", ontime), ("임박", soon), ("초과", late)):
        body = client.get("/api/v1/admin/reports", params={"sla_status": sla, "limit": 200},
                          headers=headers).json()
        assert all(i["sla_status"] == sla for i in body["items"])
        assert expected_no.display_no in {i["display_no"] for i in body["items"]}

    body = client.get("/api/v1/admin/reports", params={"status": "해결", "limit": 200}, headers=headers).json()
    assert all(i["status"] == "해결" for i in body["items"]) and body["total"] >= 1
    body = client.get("/api/v1/admin/reports", params={"priority": "P1", "sort": "sla_deadline"},
                      headers=headers).json()
    assert all(i["priority"] == "P1" for i in body["items"])

    newest = client.get("/api/v1/admin/reports", params={"limit": 1}, headers=headers).json()
    assert newest["total"] >= 4 and len(newest["items"]) == 1


def test_detail_and_status_change(client: TestClient, headers: dict[str, str], admin: Admin) -> None:
    r = make_report(created_ago_h=1, sla_h=24)
    url = f"/api/v1/admin/reports/{r.id}"
    detail = client.get(url, headers=headers).json()
    assert detail["description"] == "테스트 신고"
    assert [h["to_status"] for h in detail["status_history"]] == ["접수"]

    res = client.patch(f"{url}/status", json={"to_status": "배정", "memo": "시설팀 배정"}, headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "배정"
    last = body["status_history"][-1]
    assert (last["from_status"], last["to_status"], last["memo"]) == ("접수", "배정", "시설팀 배정")
    assert last["changed_by"] == {"id": str(admin.id), "name": "테스트관리자"}

    same = client.patch(f"{url}/status", json={"to_status": "배정"}, headers=headers)
    assert same.status_code == 409
    bad = client.patch(f"{url}/status", json={"to_status": "없는상태"}, headers=headers)
    assert bad.status_code == 422


def test_report_not_found(client: TestClient, headers: dict[str, str]) -> None:
    missing = f"/api/v1/admin/reports/{uuid.uuid4()}"
    assert client.get(missing, headers=headers).status_code == 404
    assert client.patch(f"{missing}/status", json={"to_status": "배정"}, headers=headers).status_code == 404


def test_status_full_path_and_invalid_transitions(client: TestClient, headers: dict[str, str], admin: Admin) -> None:
    """작업 1-7: 정상 경로 끝까지 + 재오픈 + 잘못된 전이 409 (명세 10-2)."""
    r = make_report(created_ago_h=1, sla_h=24)
    url = f"/api/v1/admin/reports/{r.id}"

    # 2-2·2-3: 접수 상태에서 종료 직행·건너뛰기는 409, 상태·이력 그대로
    res = client.patch(f"{url}/status", json={"to_status": "종료"}, headers=headers)
    assert res.status_code == 409
    assert "가능한 상태" in res.json()["detail"]
    assert client.patch(f"{url}/status", json={"to_status": "처리중"}, headers=headers).status_code == 409
    detail = client.get(url, headers=headers).json()
    assert detail["status"] == "접수"
    assert [h["to_status"] for h in detail["status_history"]] == ["접수"]

    # 2-1·2-4: 배정 → 처리중 → 해결 → (재오픈) 처리중 → 해결 → 종료
    for to in ("배정", "처리중", "해결", "처리중", "해결", "종료"):
        res = client.patch(f"{url}/status", json={"to_status": to}, headers=headers)
        assert res.status_code == 200, (to, res.json())
    body = res.json()
    assert body["status"] == "종료"
    assert [h["to_status"] for h in body["status_history"]] == [
        "접수", "배정", "처리중", "해결", "처리중", "해결", "종료",
    ]
    reopen = body["status_history"][4]
    assert (reopen["from_status"], reopen["to_status"]) == ("해결", "처리중")
    assert all(h["changed_by"] == {"id": str(admin.id), "name": "테스트관리자"} for h in body["status_history"][1:])

    # 2-5: 종료 후에는 어떤 변경도 409
    res = client.patch(f"{url}/status", json={"to_status": "처리중"}, headers=headers)
    assert res.status_code == 409
    assert "더 이상 바꿀 수 없어요" in res.json()["detail"]
