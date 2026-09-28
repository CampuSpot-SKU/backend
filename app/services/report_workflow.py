"""신고 상태 변경 (ESM 워크플로우: 접수 → 배정 → 처리중 → 해결 → 종료).

1-6에서는 "상태 바꾸기 + 이력 남기기"까지만 구현.
어떤 상태에서 어떤 상태로 갈 수 있는지(전이 규칙)는 작업 1-7에서 check_transition()에 채운다
(명세 10-2: 정상 경로 통과, 접수→종료 직행 같은 잘못된 전이 차단 + pytest).
"""
import uuid

from sqlalchemy.orm import Session

from app.models.enums import ReportStatus
from app.models.report import Report, ReportStatusHistory


class InvalidTransitionError(Exception):
    """허용되지 않는 상태 변경 — 라우터가 409로 응답."""


def check_transition(from_status: ReportStatus, to_status: ReportStatus) -> None:
    """상태 전이 검사. TODO(1-7): 허용 전이 규칙 추가. 지금은 같은 상태로의 변경만 막는다."""
    if from_status == to_status:
        raise InvalidTransitionError(f"이미 '{to_status.value}' 상태예요.")


def change_status(
    db: Session, report: Report, to_status: ReportStatus, memo: str | None, admin_id: uuid.UUID | None
) -> None:
    """상태 변경 + report_status_history 기록. commit은 호출한 쪽이 한다."""
    check_transition(report.status, to_status)
    db.add(
        ReportStatusHistory(
            report_id=report.id,
            from_status=report.status,
            to_status=to_status,
            memo=memo,
            changed_by=admin_id,
        )
    )
    report.status = to_status
