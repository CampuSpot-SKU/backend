"""SLA 에스컬레이션 배치 테스트 (작업 1-13, 명세서 3-1 "50%·100%·150%").

- 단계 계산·우선순위 상향·메일 문구 단위테스트: DB 없이 항상 실행 (시간은 고정값으로 — 명세 10장 "시간 mock")
- /cron/sla-check 통합테스트: TEST_DATABASE_URL(마이그레이션 끝난 테스트용 빈 DB)이 있을 때만 — 운영 DB 금지!
  다른 테스트의 처리 중 신고도 같이 잡히므로 이 테스트가 만든 신고의 결과만 비교한다. 메일은 가짜로 바꿔 기록만.
"""
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB
    os.environ["CRON_SECRET"] = "test-cron-secret"

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.models import Category, Report, ReportStatusHistory
from app.models.enums import Priority, ReportStatus
from app.services import mailer
from app.services import sla_escalation as sla

T0 = datetime(2026, 10, 12, 0, 0, tzinfo=UTC)
H = timedelta(hours=1)


# ── 단위테스트 ──

def test_step_for_thresholds() -> None:
    deadline = T0 + 24 * H
    assert sla.step_for(T0, deadline, T0 + 11.9 * H) == 0
    assert sla.step_for(T0, deadline, T0 + 12 * H) == 50       # 절반 = 관리자 목록 "임박"과 같은 기준
    assert sla.step_for(T0, deadline, T0 + 24 * H) == 100      # 마감 정각
    assert sla.step_for(T0, deadline, T0 + 35.9 * H) == 100
    assert sla.step_for(T0, deadline, T0 + 36 * H) == 150
    assert sla.step_for(T0, T0, T0) == 150                     # 마감이 접수 시각과 같으면 바로 초과
    assert sla.step_for(T0, T0, T0 - H) == 0


def test_raised_priority() -> None:
    assert [sla.raised(p) for p in Priority] == [Priority.P1, Priority.P1, Priority.P2, Priority.P3]


def _esc(step: int, no: int = 7, before: Priority = Priority.P3, after: Priority = Priority.P3) -> sla.Escalation:
    return sla.Escalation(report_id=uuid.uuid4(), display_no=no, step=step, previous_step=0, priority_before=before,
                          priority_after=after, where="북악관 3층 화장실", category="시설·설비",
                          description="세면대 물이 안 멈춰요", sla_deadline=T0 + 72 * H)


def test_mail_subject_and_body() -> None:
    escs = [_esc(50, 1), _esc(100, 2, Priority.P3, Priority.P2), _esc(150, 3)]
    assert sla.subject(escs) == "[긴급] 캠퍼스팟 처리 지연 신고 3건"
    assert sla.subject([escs[0]]) == "[리마인드] 캠퍼스팟 처리 지연 신고 1건"
    body = sla.mail_body(escs, {(Priority.P3, 100): "시설팀장에게 전화"}, T0)
    assert "2026-10-12 09:00 KST" in body
    assert body.index("관리자 긴급 알림") < body.index("부서장 알림") < body.index("리마인드")  # 급한 단계부터
    assert "2번 [P3→P2 상향] 북악관 3층 화장실 · 시설·설비" in body
    assert "조치: 시설팀장에게 전화" in body and "조치: 리마인드" in body  # 설정 문구, 없으면 기본 문구
    assert get_settings().admin_page_url in body


# ── 통합테스트 ──

needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL 없음 — DB 통합테스트 생략")
URL = "/api/v1/cron/sla-check"
SECRET = {"X-Cron-Secret": "test-cron-secret"}


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    box: list[tuple[str, str]] = []

    def fake(subject: str, body: str) -> int:
        box.append((subject, body))
        return 4

    monkeypatch.setattr(mailer, "send_alert", fake)
    return box


@pytest.fixture()
def reports() -> Iterator[dict[str, uuid.UUID]]:
    """접수 후 경과율이 정해진 신고들 (SLA 24h). 이름 → id. 끝나면 지움(이력은 CASCADE)."""
    now = datetime.now(UTC)
    db = get_sessionmaker()()
    cat = db.scalar(select(Category).where(Category.name == "기타"))
    assert cat is not None

    def add(name: str, hours_ago: float, priority: Priority = Priority.P3,
            status: ReportStatus = ReportStatus.RECEIVED, level: int = 0) -> uuid.UUID:
        created = now - hours_ago * H
        r = Report(category_id=cat.id, priority=priority, status=status, description=f"SLA테스트 {name}",
                   location_raw="테스트 위치", created_at=created, sla_deadline=created + 24 * H,
                   escalation_level=level)
        db.add(r)
        db.flush()
        return r.id

    ids = {
        "fresh": add("fresh", 2),                                    # 8% — 아무것도 안 함
        "half": add("half", 13),                                     # 54% → 50
        "late": add("late", 25, Priority.P3, ReportStatus.IN_PROGRESS),  # 104% → 100 + P3→P2
        "very_late": add("very_late", 40, Priority.P4),              # 167%, 처음 → 150 하나로 묶고 P4→P3
        "done": add("done", 40, status=ReportStatus.RESOLVED),       # 해결 → 제외
        "already": add("already", 25, Priority.P2, level=100),       # 이미 100 처리 → 그대로
    }
    db.commit()
    db.close()
    yield ids
    db = get_sessionmaker()()
    db.execute(delete(Report).where(Report.id.in_(ids.values())))
    db.commit()
    db.close()


def _state(ids: dict[str, uuid.UUID]) -> dict[str, tuple[int, Priority]]:
    db = get_sessionmaker()()
    rows = {r.id: (r.escalation_level, r.priority) for r in db.scalars(select(Report).where(Report.id.in_(ids.values())))}
    db.close()
    return {k: rows[v] for k, v in ids.items()}


def _mine(body: dict[str, Any], ids: dict[str, uuid.UUID]) -> dict[int, dict[str, Any]]:
    db = get_sessionmaker()()
    nos = {r.display_no for r in db.scalars(select(Report).where(Report.id.in_(ids.values())))}
    db.close()
    return {i["display_no"]: i for i in body["items"] if i["display_no"] in nos}


@needs_db
def test_requires_secret(client: TestClient) -> None:
    assert client.post(URL).status_code == 401
    assert client.post(URL, headers={"X-Cron-Secret": "wrong"}).status_code == 401


@needs_db
def test_dry_run_changes_nothing(client: TestClient, reports: dict[str, uuid.UUID],
                                 sent: list[tuple[str, str]]) -> None:
    before = _state(reports)
    res = client.post(URL, params={"dry_run": "true"}, headers=SECRET)
    assert res.status_code == 200, res.text
    assert res.json()["mail"] == "dry_run" and len(_mine(res.json(), reports)) == 3
    assert _state(reports) == before and sent == []


@needs_db
def test_escalates_once_per_step(client: TestClient, reports: dict[str, uuid.UUID],
                                 sent: list[tuple[str, str]]) -> None:
    res = client.post(URL, headers=SECRET)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mail"] == "sent:4" and len(sent) == 1
    assert len(_mine(body, reports)) == 3
    st = _state(reports)
    assert st["fresh"] == (0, Priority.P3)
    assert st["half"] == (50, Priority.P3)
    assert st["late"] == (100, Priority.P2)
    assert st["very_late"] == (150, Priority.P3)
    assert st["done"] == (0, Priority.P3)
    assert st["already"] == (100, Priority.P2)
    subject, text = sent[0]
    assert subject.startswith("[긴급]") and "SLA테스트 very_late" in text and "P3→P2 상향" in text

    db = get_sessionmaker()()
    memos = db.scalars(select(ReportStatusHistory.memo).where(ReportStatusHistory.report_id == reports["late"])).all()
    db.close()
    assert memos == ["SLA 마감 초과 — 우선순위 P3→P2 자동 상향"]

    again = client.post(URL, headers=SECRET).json()  # 같은 단계는 다시 안 보냄
    assert _mine(again, reports) == {}
    assert _state(reports) == st


@needs_db
def test_mail_failure_saves_nothing(client: TestClient, reports: dict[str, uuid.UUID],
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(subject: str, body: str) -> int:
        raise mailer.MailError("SMTPAuthenticationError")

    monkeypatch.setattr(mailer, "send_alert", broken)
    before = _state(reports)
    assert client.post(URL, headers=SECRET).status_code == 502
    assert _state(reports) == before


@needs_db
def test_mail_not_configured_still_escalates(client: TestClient, reports: dict[str, uuid.UUID],
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    def unset(subject: str, body: str) -> int:
        raise mailer.MailNotConfigured("비어 있음")

    monkeypatch.setattr(mailer, "send_alert", unset)
    res = client.post(URL, headers=SECRET)
    assert res.status_code == 200 and res.json()["mail"] == "skipped:not_configured"
    assert _state(reports)["half"] == (50, Priority.P3)
