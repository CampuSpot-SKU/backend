"""Cloud Scheduler 전용 — SLA/에스컬레이션 체크 (작업 1-13, 명세서 3-1·5-1). reports 도메인이라 backend에 남음.

반복탐지/재발예측/공지크롤링은 ai 서비스의 /api/v1/cron/* 로 이전됨.
POST /cron/sla-check — X-Cron-Secret 헤더(CRON_SECRET)가 맞아야 실행. ?dry_run=true면 계산만(저장·메일 없음).
단계·중복 방지 규칙은 services/sla_escalation.py 맨 위 주석.
"""
import hmac
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from app.config import get_settings
from app.deps import DbSession
from app.models.enums import Priority
from app.services import mailer
from app.services import sla_escalation as sla

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cron", tags=["cron"])


class EscalationOut(BaseModel):
    display_no: int
    step: int
    previous_step: int
    priority_before: Priority
    priority_after: Priority


class SlaCheckOut(BaseModel):
    checked: int  # 살펴본 처리 중 신고 수 (마감 있고 150% 단계 전)
    escalated: int
    mail: str  # sent:N / skipped:not_configured / none / dry_run
    items: list[EscalationOut]


def _check_secret(given: str | None) -> None:
    expected = get_settings().cron_secret
    if not expected:
        raise HTTPException(status_code=503, detail="CRON_SECRET 미설정")
    if not given or not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="X-Cron-Secret이 맞지 않아요")


_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"description": "X-Cron-Secret 없음·틀림"},
    502: {"description": "알림 메일 발송 실패 — 아무것도 저장 안 됨(다음 실행에 다시)"},
    503: {"description": "CRON_SECRET 미설정"},
}


@router.post("/sla-check", response_model=SlaCheckOut, responses=_ERRORS)
def sla_check(
    db: DbSession,
    x_cron_secret: Annotated[str | None, Header()] = None,
    dry_run: Annotated[bool, Query(description="true면 계산만 하고 저장·메일 없음")] = False,
) -> SlaCheckOut:
    """처리 중 신고의 SLA 경과율을 보고 50%·100%·150% 단계 알림(메일 한 통) + 100%에서 우선순위 상향."""
    _check_secret(x_cron_secret)
    try:
        result = sla.run(db, dry_run=dry_run)
    except mailer.MailError as e:
        db.rollback()
        logger.error("SLA 알림 메일 실패: %s", e)
        raise HTTPException(status_code=502, detail="알림 메일을 보내지 못했어요") from None
    if not dry_run:
        db.commit()
    return SlaCheckOut(
        checked=result.checked,
        escalated=len(result.escalations),
        mail=result.mail,
        items=[
            EscalationOut(display_no=e.display_no, step=e.step, previous_step=e.previous_step,
                          priority_before=e.priority_before, priority_after=e.priority_after)
            for e in result.escalations
        ],
    )
