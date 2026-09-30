"""신고(reports) 접수 생성 — 카테고리·우선순위·SLA 마감시각 결정 후 저장 (작업 1-3).

우선순위·SLA는 코드 상수가 아니라 설정 테이블(priority_matrix_rules, sla_config)에서 읽는다
(명세서 5장 "학교별 확장성"). commit은 호출한 쪽(라우터)이 한다.
"""
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.config import Building, Category, PriorityMatrixRule, SlaConfig
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


def _sla_deadline(db: Session, priority: Priority, now: datetime) -> datetime | None:
    sla = db.scalar(select(SlaConfig).where(SlaConfig.priority == priority))
    if sla is None:
        logger.warning("SLA 설정 없음 (%s)", priority)
        return None
    return now + timedelta(hours=sla.sla_hours)


def create_report(
    db: Session, session_id: uuid.UUID, slots: ReportSlots, description: str
) -> tuple[Report, Category]:
    """접수 생성 + 최초 상태 이력(null → 접수) 기록. display_no는 DB가 순번으로 채움."""
    category = _category(db, slots.category)
    priority = _priority(db, slots)
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
    return report, category
