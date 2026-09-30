"""신고 상태 변경 (ESM 워크플로우: 접수 → 배정 → 처리중 → 해결 → 종료).

허용 전이는 ALLOWED_TRANSITIONS 표 (명세 10-2, 작업 1-7): 한 단계씩 앞으로만 가고,
뒤로 가기는 해결 → 처리중(재오픈) 하나만 허용. 그 밖의 변경(같은 상태·건너뛰기·뒤로 가기·
종료 후 변경)은 InvalidTransitionError → 라우터가 409로 응답한다.
재오픈해도 sla_deadline은 다시 계산하지 않는다 (원래 마감 기준 — 명세 10-2).
"""
import uuid

from sqlalchemy.orm import Session

from app.models.enums import ReportStatus
from app.models.report import Report, ReportStatusHistory


class InvalidTransitionError(Exception):
    """허용되지 않는 상태 변경 — 라우터가 409로 응답."""


# 허용 전이 표 (명세 10-2, 2026-09-30 결정): 정상 경로는 한 단계씩 앞으로,
# 해결 → 처리중(재오픈)만 뒤로 갈 수 있다. 튜플 순서는 오류 메시지의 안내 순서.
ALLOWED_TRANSITIONS: dict[ReportStatus, tuple[ReportStatus, ...]] = {
    ReportStatus.RECEIVED: (ReportStatus.ASSIGNED,),
    ReportStatus.ASSIGNED: (ReportStatus.IN_PROGRESS,),
    ReportStatus.IN_PROGRESS: (ReportStatus.RESOLVED,),
    ReportStatus.RESOLVED: (ReportStatus.CLOSED, ReportStatus.IN_PROGRESS),
    ReportStatus.CLOSED: (),
}


def check_transition(from_status: ReportStatus, to_status: ReportStatus) -> None:
    """상태 전이 검사. 허용되지 않으면 InvalidTransitionError (메시지는 관리자 화면에 그대로 표시)."""
    if from_status == to_status:
        raise InvalidTransitionError(f"이미 '{to_status.value}' 상태예요.")
    allowed = ALLOWED_TRANSITIONS[from_status]
    if to_status in allowed:
        return
    if not allowed:
        raise InvalidTransitionError(f"'{from_status.value}' 상태의 신고는 더 이상 바꿀 수 없어요.")
    nexts = ", ".join(f"'{s.value}'" for s in allowed)
    raise InvalidTransitionError(
        f"'{from_status.value}'에서 '{to_status.value}'(으)로는 바꿀 수 없어요. 가능한 상태: {nexts}"
    )


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
