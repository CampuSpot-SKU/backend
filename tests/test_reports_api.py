"""본인 신고 조회 API 통합테스트 — GET /reports/{display_no}?session_id= (작업 1-12).

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

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.models import Category, Report, ReportStatusHistory
from app.models.chat import ChatSession
from app.models.enums import Priority, ReportStatus


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    with TestClient(app) as c:
        yield c


def make_session() -> uuid.UUID:
    db = get_sessionmaker()()
    s = ChatSession(user_identifier=f"test-{uuid.uuid4().hex[:8]}")
    db.add(s)
    db.commit()
    sid = s.id
    db.close()
    return sid


def make_report(session_id: uuid.UUID) -> int:
    """신고 + 접수→배정 이력(관리자 메모 포함)을 넣고 접수번호를 돌려줌."""
    db = get_sessionmaker()()
    cat = db.scalar(select(Category).where(Category.name == "시설·설비"))
    assert cat is not None
    r = Report(session_id=session_id, category_id=cat.id, priority=Priority.P2,
               status=ReportStatus.ASSIGNED, description="테스트 신고")
    db.add(r)
    db.flush()
    db.add(ReportStatusHistory(report_id=r.id, from_status=None, to_status=ReportStatus.RECEIVED))
    db.add(ReportStatusHistory(report_id=r.id, from_status=ReportStatus.RECEIVED,
                               to_status=ReportStatus.ASSIGNED, memo="내부 메모: 시설팀 김OO 배정"))
    db.commit()
    no = r.display_no
    db.close()
    return no


def test_owner_can_see_status_and_history(client: TestClient) -> None:
    sid = make_session()
    no = make_report(sid)
    res = client.get(f"/api/v1/reports/{no}", params={"session_id": str(sid)})
    assert res.status_code == 200
    body = res.json()
    assert body["display_no"] == no and body["status"] == "배정" and body["priority"] == "P2"
    assert body["category"]["name"] == "시설·설비"
    assert [(h["from_status"], h["to_status"]) for h in body["status_history"]] == [
        (None, "접수"), ("접수", "배정")]
    # 관리자 내부 메모·담당자는 학생 화면에 내보내지 않음
    assert "memo" not in res.text and "changed_by" not in res.text and "김OO" not in res.text


def test_other_session_gets_404_same_as_missing(client: TestClient) -> None:
    owner, stranger = make_session(), make_session()
    no = make_report(owner)
    other = client.get(f"/api/v1/reports/{no}", params={"session_id": str(stranger)})
    missing = client.get("/api/v1/reports/999999999", params={"session_id": str(owner)})
    assert other.status_code == 404 and missing.status_code == 404
    assert other.json() == missing.json()  # 존재 여부를 구분할 수 없게 응답이 같아야 함


def test_bad_parameters_are_422(client: TestClient) -> None:
    sid = str(make_session())
    assert client.get("/api/v1/reports/abc", params={"session_id": sid}).status_code == 422
    assert client.get("/api/v1/reports/1", params={"session_id": "not-a-uuid"}).status_code == 422
    assert client.get("/api/v1/reports/1").status_code == 422  # session_id 없음
