"""신고(reports) 접수 생성 — 카테고리·우선순위·SLA 마감시각 결정 후 저장 (작업 1-3).

우선순위·SLA는 코드 상수가 아니라 설정 테이블(priority_matrix_rules, sla_config)에서 읽는다
(명세서 5장 "학교별 확장성"). 매트릭스 결과는 켜진 조건-액션 규칙(automation_rules, 1-21)이 맞으면
규칙 값으로 바뀐다(명세 3-1). commit은 호출한 쪽(라우터)이 한다.
"""
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.config import AutomationRule, Building, Category, PriorityMatrixRule, SlaConfig
from app.models.enums import Priority, ReportStatus
from app.models.report import Report, ReportStatusHistory
from app.services.slot_filling import DEFAULT_CATEGORY, BuildingRef, ReportSlots

logger = logging.getLogger(__name__)

# 설정 테이블에 해당 조합이 없을 때만 쓰는 안전값 (정상이라면 0002 시드로 항상 존재)
FALLBACK_PRIORITY = Priority.P3


def load_buildings(db: Session) -> list[BuildingRef]:
    rows = db.scalars(select(Building)).all()
    return [BuildingRef(id=b.id, name=b.name, aliases=tuple(b.aliases or ())) for b in rows]


def load_category_names(db: Session) -> list[str]:
    """AI가 고를 수 있는 카테고리 이름들 (사용 중인 것만) — 학교마다 달라질 수 있어 하드코딩하지 않음."""
    return list(db.scalars(select(Category.name).where(Category.is_active.is_(True))).all())


def _category(db: Session, name: str | None) -> Category:
    for n in (name, DEFAULT_CATEGORY):
        if n is None:
            continue
        cat = db.scalar(select(Category).where(Category.name == n, Category.is_active.is_(True)))
        if cat is not None:
            return cat
    raise RuntimeError(f"카테고리 '{DEFAULT_CATEGORY}'가 categories 테이블에 없습니다.")


def _priority(db: Session, slots: ReportSlots) -> Priority:
    rule = db.scalar(
        select(PriorityMatrixRule).where(
            PriorityMatrixRule.impact == slots.impact,
            PriorityMatrixRule.urgency == slots.urgency,
        )
    )
    if rule is None:
        logger.warning("우선순위 매트릭스 규칙 없음 (%s, %s)", slots.impact, slots.urgency)
        return FALLBACK_PRIORITY
    return rule.resulting_priority


PRIORITY_RANK = {p: i for i, p in enumerate(Priority)}  # P1이 0 (가장 높음)


def matching_rule(
    rules: list[AutomationRule], category_id: uuid.UUID, building_id: uuid.UUID | None
) -> AutomationRule | None:
    """켜진 규칙 중 카테고리가 같고 건물이 비었거나 같은 것. 여러 개면 건물 지정 규칙 → 더 높은 우선순위 (명세 3-1)."""
    hits = [
        r for r in rules
        if r.is_active and r.category_id == category_id and r.building_id in (None, building_id)
    ]
    return min(hits, key=lambda r: (r.building_id is None, PRIORITY_RANK[r.resulting_priority]), default=None)


def rule_note(rule: AutomationRule | None) -> str:
    """판정 이유 끝에 붙이는 한 줄 (명세 3-1) — 규칙이 우선순위를 정했으면 " 규칙 '이름' 적용." """
    return f" 규칙 '{rule.name}' 적용." if rule is not None else ""


def _sla_deadline(db: Session, priority: Priority, now: datetime) -> datetime | None:
    sla = db.scalar(select(SlaConfig).where(SlaConfig.priority == priority))
    if sla is None:
        logger.warning("SLA 설정 없음 (%s)", priority)
        return None
    return now + timedelta(hours=sla.sla_hours)


def create_report(
    db: Session, session_id: uuid.UUID, slots: ReportSlots, description: str
) -> tuple[Report, Category, AutomationRule | None]:
    """접수 생성 + 최초 상태 이력(null → 접수) 기록. display_no는 DB가 순번으로 채움.
    세 번째 값은 우선순위를 바꾼 조건-액션 규칙(없으면 None) — 판정 이유 끝에 "규칙 '이름' 적용"을 붙이는 데 씀."""
    category = _category(db, slots.category)
    priority = _priority(db, slots)
    rules = list(db.scalars(select(AutomationRule).where(AutomationRule.is_active.is_(True))).all())
    rule = matching_rule(rules, category.id, slots.building_id)
    if rule is not None:
        priority = rule.resulting_priority
    now = datetime.now(UTC)
    report = Report(
        session_id=session_id,
        category_id=category.id,
        priority=priority,
        status=ReportStatus.RECEIVED,
        building_id=slots.building_id,
        floor=slots.floor,
        detail=slots.detail,
        # 건물을 목록에서 못 찾았으면 사람이 읽을 위치 문자열을 그대로 남김 (관리자 확인용)
        location_raw=None if slots.building_id else slots.location_text,
        description=description,
        sla_deadline=_sla_deadline(db, priority, now),
    )
    db.add(report)
    db.flush()
    db.refresh(report)  # DB가 만든 display_no·created_at 읽어오기
    db.add(
        ReportStatusHistory(
            report_id=report.id, from_status=None, to_status=ReportStatus.RECEIVED, memo="챗봇 접수"
        )
    )
    return report, category, rule
