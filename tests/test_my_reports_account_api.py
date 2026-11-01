"""로그인 계정 기준 본인 신고 조회 통합테스트 — GET /reports/mine, GET /reports/{no} (학생 토큰) (11/1 추가).

TEST_DATABASE_URL(마이그레이션 끝난 테스트용 빈 DB)이 있을 때만 실행 — 운영 DB 금지!
"""
import os
import uuid
from collections.abc import Iterator

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
from app.models import Category, Report, ReportStatusHistory
from app.models.chat import ChatSession
from app.models.enums import Priority, ReportStatus

PASSWORD = "pass1234!"


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    with TestClient(app) as c:
        yield c


def _signup(client: TestClient) -> dict[str, str]:
    login_id = f"t{uuid.uuid4().hex[:10]}"
    res = client.post("/api/v1/auth/signup", json={"login_id": login_id, "password": PASSWORD, "name": "김학생"})
    assert res.status_code in (200, 201), res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _report_in_new_session() -> tuple[uuid.UUID, int]:
    db = get_sessionmaker()()
    s = ChatSession(user_identifier=f"test-{uuid.uuid4().hex[:8]}")
    db.add(s)
    db.flush()
    cat = db.scalar(select(Category).where(Category.name == "시설·설비"))
    assert cat is not None
    r = Report(session_id=s.id, category_id=cat.id, priority=Priority.P2, status=ReportStatus.ASSIGNED,
               description="계정 조회 테스트 신고", assigned_dept="시설팀")
    db.add(r)
    db.flush()
    db.add(ReportStatusHistory(report_id=r.id, from_status=None, to_status=ReportStatus.RECEIVED))
    db.add(ReportStatusHistory(report_id=r.id, from_status=ReportStatus.RECEIVED,
                               to_status=ReportStatus.ASSIGNED, memo="내부 메모: 김OO"))
    db.commit()
    sid, no = s.id, r.display_no
    db.close()
    return sid, no


def test_logged_in_student_tracks_claimed_reports_from_any_device(client: TestClient) -> None:
    me, stranger = _signup(client), _signup(client)
    sid, no = _report_in_new_session()
    assert client.post(f"/api/v1/chat/sessions/{sid}/claim", headers=me).status_code == 204

    mine = client.get("/api/v1/reports/mine", headers=me)
    assert mine.status_code == 200
    assert [r["display_no"] for r in mine.json()["reports"]] == [no]

    # session_id 없이 토큰만으로 상세 조회 (다른 기기)
    res = client.get(f"/api/v1/reports/{no}", headers=me)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "배정" and body["assigned_dept"] == "시설팀"
    assert body["description"] == "계정 조회 테스트 신고"
    assert len(body["status_history"]) == 2
    assert "김OO" not in res.text and "memo" not in res.text

    # 다른 계정은 목록에도 없고 상세는 404
    assert client.get("/api/v1/reports/mine", headers=stranger).json()["reports"] == []
    assert client.get(f"/api/v1/reports/{no}", headers=stranger).status_code == 404


def test_mine_requires_login(client: TestClient) -> None:
    assert client.get("/api/v1/reports/mine").status_code == 401
    assert client.get("/api/v1/reports/1").status_code == 422
