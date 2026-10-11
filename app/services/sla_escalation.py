"""SLA 에스컬레이션 배치 — 처리 중인 신고의 SLA 경과율을 보고 단계별로 알림 (작업 1-13, 명세서 3-1).

단계 (경과율 = (지금 − 접수) ÷ (마감 − 접수)):
  50%  → 리마인드 메일
  100% → 부서장 알림 메일 + 우선순위 한 단계 상향(P4→P3→P2→P1, P1은 그대로)
  150% → 관리자 긴급 알림 메일
- 처리 중(접수·배정·처리중)이고 sla_deadline이 있는 신고만. 50% 기준은 관리자 목록의 "임박"과 같다(report_query).
- reports.escalation_level에 마지막으로 처리한 단계를 저장해 같은 단계 알림은 한 번만 보낸다.
  배치가 밀려 여러 단계를 한꺼번에 넘었으면 가장 높은 단계 하나로 묶는다(밀린 단계를 하나씩 보내지 않음).
  100%를 넘은 적이 있으면 우선순위 상향은 150%로 바로 건너뛰어도 한 번 한다.
- 한 번 실행에 메일 한 통(단계별로 묶은 요약) — 신고마다 메일을 보내지 않는다.
- 마감(sla_deadline)은 다시 계산하지 않는다(명세 5-1 SLA 설정과 같은 원칙). 우선순위 상향은 상태 이력에 메모로 남긴다.
- 메일 설정이 비어 있으면 메일만 건너뛰고 단계는 진행한다. 메일 발송이 실패하면 아무것도 저장하지 않는다(다음 실행에 다시).
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.config import Building, Category, SlaConfig
from app.models.enums import Priority
from app.models.report import Report, ReportStatusHistory
from app.services import mailer
from app.services.report_query import OPEN_STATUSES

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
STEPS = (150, 100, 50)  # 높은 단계부터 검사
STEP_TITLE = {50: "리마인드 (SLA 50% 경과)", 100: "부서장 알림 (SLA 마감 초과)", 150: "관리자 긴급 알림 (SLA 150% 초과)"}
DEFAULT_ACTION = {50: "리마인드", 100: "부서장 알림 + 우선순위 상향", 150: "관리자 긴급알림"}


def step_for(created_at: datetime, deadline: datetime, now: datetime) -> int:
    """지금 도달한 단계 (0·50·100·150)."""
    total = (deadline - created_at).total_seconds()
    if total <= 0:
        return 150 if now >= deadline else 0
    ratio = (now - created_at).total_seconds() / total
    return next((s for s in STEPS if ratio * 100 >= s), 0)


def raised(priority: Priority) -> Priority:
    """한 단계 높은 우선순위 (P1은 그대로)."""
    order = list(Priority)
    return order[max(order.index(priority) - 1, 0)]


@dataclass
class Escalation:
    report_id: uuid.UUID
    display_no: int
    step: int  # 이번에 처리할 단계
    previous_step: int
    priority_before: Priority
    priority_after: Priority
    where: str
    category: str
    description: str
    sla_deadline: datetime


@dataclass
class SlaCheckResult:
    checked: int  # 살펴본 처리 중 신고 수
    escalations: list[Escalation]
    mail: str  # "sent:N" / "skipped:<이유>" / "none"(보낼 것 없음) / "dry_run"


def plan(db: Session, now: datetime) -> tuple[int, list[Escalation]]:
    rows = db.execute(
        select(Report, Category.name, Building.name)
        .join(Category, Report.category_id == Category.id)
        .outerjoin(Building, Report.building_id == Building.id)
        .where(Report.status.in_(OPEN_STATUSES), Report.sla_deadline.is_not(None), Report.escalation_level < 150)
        .order_by(Report.sla_deadline)
    ).all()
    out: list[Escalation] = []
    for report, category, building in rows:
        assert report.sla_deadline is not None
        step = step_for(report.created_at, report.sla_deadline, now)
        if step <= report.escalation_level:
            continue
        bump = step >= 100 and report.escalation_level < 100
        where = " ".join(p for p in (building, f"{report.floor}층" if report.floor else None, report.detail) if p)
        out.append(Escalation(
            report_id=report.id,
            display_no=report.display_no,
            step=step,
            previous_step=report.escalation_level,
            priority_before=report.priority,
            priority_after=raised(report.priority) if bump else report.priority,
            where=where or report.location_raw or "위치 미상",
            category=category,
            description=report.description,
            sla_deadline=report.sla_deadline,
        ))
    return len(rows), out


def _actions(db: Session) -> dict[tuple[Priority, int], str]:
    out: dict[tuple[Priority, int], str] = {}
    for c in db.scalars(select(SlaConfig)).all():
        for step, text in ((50, c.escalation_50pct_action), (100, c.escalation_100pct_action),
                           (150, c.escalation_150pct_action)):
            out[(c.priority, step)] = text or DEFAULT_ACTION[step]
    return out


def mail_body(escalations: list[Escalation], actions: dict[tuple[Priority, int], str], now: datetime) -> str:
    lines = [f"캠퍼스팟 SLA 점검 결과예요 ({now.astimezone(KST):%Y-%m-%d %H:%M} KST).", ""]
    for step in STEPS:
        group = [e for e in escalations if e.step == step]
        if not group:
            continue
        lines.append(f"■ {STEP_TITLE[step]} — {len(group)}건")
        for e in group:
            prio = (f"{e.priority_before.value}→{e.priority_after.value} 상향"
                    if e.priority_after != e.priority_before else e.priority_after.value)
            lines.append(f"- {e.display_no}번 [{prio}] {e.where} · {e.category} — {e.description[:60]}")
            lines.append(f"  마감 {e.sla_deadline.astimezone(KST):%m/%d %H:%M} · 조치: "
                         f"{actions.get((e.priority_before, step), DEFAULT_ACTION[step])}")
        lines.append("")
    lines.append(f"관리자 화면: {get_settings().admin_page_url}")
    lines.append("이 메일은 SLA 배치가 자동으로 보냈어요. 같은 단계 알림은 신고마다 한 번만 가요.")
    return "\n".join(lines)


def subject(escalations: list[Escalation]) -> str:
    top = max(e.step for e in escalations)
    tag = {150: "[긴급]", 100: "[SLA 초과]", 50: "[리마인드]"}[top]
    return f"{tag} 캠퍼스팟 처리 지연 신고 {len(escalations)}건"


def run(db: Session, now: datetime | None = None, dry_run: bool = False) -> SlaCheckResult:
    """commit은 호출한 쪽(라우터)이 한다. 메일 발송 실패는 MailError로 그대로 올림(저장 전)."""
    now = now or datetime.now(UTC)
    checked, escalations = plan(db, now)
    if dry_run:
        return SlaCheckResult(checked=checked, escalations=escalations, mail="dry_run")
    if not escalations:
        return SlaCheckResult(checked=checked, escalations=[], mail="none")
    try:
        sent = mailer.send_alert(subject(escalations), mail_body(escalations, _actions(db), now))
        mail = f"sent:{sent}"
    except mailer.MailNotConfigured as err:
        logger.warning("SLA 알림 메일 건너뜀: %s", err)
        mail = "skipped:not_configured"
    reports = {r.id: r for r in db.scalars(
        select(Report).where(Report.id.in_([e.report_id for e in escalations])).with_for_update()).all()}
    for e in escalations:
        r = reports[e.report_id]
        r.escalation_level = e.step
        if e.priority_after != r.priority:
            r.priority = e.priority_after
            db.add(ReportStatusHistory(
                report_id=r.id, from_status=r.status, to_status=r.status, changed_by=None,
                memo=f"SLA 마감 초과 — 우선순위 {e.priority_before.value}→{e.priority_after.value} 자동 상향",
            ))
    db.flush()
    logger.info("SLA 점검: 처리 중 %d건 중 %d건 에스컬레이션, 메일 %s", checked, len(escalations), mail)
    return SlaCheckResult(checked=checked, escalations=escalations, mail=mail)
