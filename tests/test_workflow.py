"""신고 상태 전이 규칙 테스트 — 허용 전이 표(명세 10-2)·409 메시지·원자성 (작업 1-7). DB 없이 도는 테스트."""
import itertools
import uuid
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from app.models.enums import ReportStatus
from app.models.report import Report, ReportStatusHistory
from app.services.report_workflow import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    change_status,
    check_transition,
)

R, A, P, V, C = (
    ReportStatus.RECEIVED,
    ReportStatus.ASSIGNED,
    ReportStatus.IN_PROGRESS,
    ReportStatus.RESOLVED,
    ReportStatus.CLOSED,
)

# 명세 10-2의 허용 전이 5개 (정상 경로 4 + 재오픈 1) — 이 외에는 전부 409
EXPECTED_ALLOWED = {(R, A), (A, P), (P, V), (V, C), (V, P)}


@pytest.mark.parametrize(("from_s", "to_s"), [(R, A), (A, P), (P, V), (V, C)])
def test_normal_path_allowed(from_s: ReportStatus, to_s: ReportStatus) -> None:
    """정상 경로: 한 단계씩 앞으로."""
    check_transition(from_s, to_s)


def test_reopen_allowed() -> None:
    """재오픈: 해결 → 처리중만 뒤로 갈 수 있음."""
    check_transition(V, P)


@pytest.mark.parametrize("status", list(ReportStatus))
def test_same_status_rejected(status: ReportStatus) -> None:
    """같은 상태로의 변경은 기존 메시지 그대로 409."""
    with pytest.raises(InvalidTransitionError) as e:
        check_transition(status, status)
    assert str(e.value) == f"이미 '{status.value}' 상태예요."


@pytest.mark.parametrize(("from_s", "to_s"), [(R, P), (R, V), (R, C), (A, V), (A, C), (P, C)])
def test_skip_rejected(from_s: ReportStatus, to_s: ReportStatus) -> None:
    """건너뛰기 금지 (접수→종료 직행이 명세 10-2 대표 예시)."""
    with pytest.raises(InvalidTransitionError):
        check_transition(from_s, to_s)


@pytest.mark.parametrize(("from_s", "to_s"), [(A, R), (P, R), (P, A), (V, R), (V, A)])
def test_backward_rejected(from_s: ReportStatus, to_s: ReportStatus) -> None:
    """뒤로 가기 금지 (재오픈 해결→처리중만 예외)."""
    with pytest.raises(InvalidTransitionError):
        check_transition(from_s, to_s)


@pytest.mark.parametrize("to_s", [R, A, P, V])
def test_closed_is_final(to_s: ReportStatus) -> None:
    """종료는 최종 상태."""
    with pytest.raises(InvalidTransitionError) as e:
        check_transition(C, to_s)
    assert "더 이상 바꿀 수 없어요" in str(e.value)


@pytest.mark.parametrize(("from_s", "to_s"), list(itertools.product(ReportStatus, ReportStatus)))
def test_all_pairs_match_table(from_s: ReportStatus, to_s: ReportStatus) -> None:
    """25개 조합 전수 검사: 허용은 정확히 5개."""
    if (from_s, to_s) in EXPECTED_ALLOWED:
        check_transition(from_s, to_s)
    else:
        with pytest.raises(InvalidTransitionError):
            check_transition(from_s, to_s)


def test_table_matches_spec() -> None:
    """코드의 표 자체가 명세 10-2와 같은지 (모든 상태가 키로 있음)."""
    assert set(ALLOWED_TRANSITIONS) == set(ReportStatus)
    assert {(f, t) for f, ts in ALLOWED_TRANSITIONS.items() for t in ts} == EXPECTED_ALLOWED


def test_error_message_lists_allowed() -> None:
    """오류 메시지에 현재·요청 상태와 가능한 상태가 한국어로 들어감 (관리자 화면에 그대로 표시)."""
    with pytest.raises(InvalidTransitionError) as e:
        check_transition(R, C)
    msg = str(e.value)
    assert msg == "'접수'에서 '종료'(으)로는 바꿀 수 없어요. 가능한 상태: '배정'"


class FakeSession:
    """add()만 기록하는 가짜 세션."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def test_change_status_rejected_keeps_state() -> None:
    """검사에서 막히면 상태도 이력도 바뀌지 않음 (원자성)."""
    db = FakeSession()
    report = Report(id=uuid.uuid4(), status=R)
    with pytest.raises(InvalidTransitionError):
        change_status(cast(Session, db), report, C, None, None)
    assert report.status == R
    assert db.added == []


def test_change_status_success_adds_history() -> None:
    """허용된 변경이면 상태가 바뀌고 이력 한 줄이 추가됨."""
    db = FakeSession()
    report = Report(id=uuid.uuid4(), status=R)
    admin_id = uuid.uuid4()
    change_status(cast(Session, db), report, A, "시설팀 배정", admin_id)
    assert report.status == A
    assert len(db.added) == 1
    h = db.added[0]
    assert isinstance(h, ReportStatusHistory)
    assert (h.report_id, h.from_status, h.to_status, h.memo, h.changed_by) == (report.id, R, A, "시설팀 배정", admin_id)
