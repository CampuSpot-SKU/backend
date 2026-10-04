"""관리자 설정 조회·저장 — 카테고리·건물·우선순위 매트릭스·SLA·탐지 임계치 (작업 1-17, 명세서 5-1).

새 신고는 접수할 때마다 이 설정 테이블을 읽으므로(report_service.py), 여기서 저장하면 다음 신고부터 반영된다.
PUT은 전체를 한 번에 검사한 뒤 전부 저장하거나(커밋은 라우터) 전부 거절한다(ConfigRuleError → 409).
"""
import uuid
from collections.abc import Sequence

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.models.config import Building, Category, DetectionConfig, PriorityMatrixRule, SlaConfig
from app.models.enums import Level, Priority
from app.models.problem import PredictionStat, ProblemCluster
from app.models.report import Report
from app.schemas.config import (
    BuildingIn,
    BuildingItem,
    CategoryIn,
    CategoryItem,
    DetectionSettings,
    PriorityRuleItem,
    SlaItem,
)
from app.services.slot_filling import DEFAULT_CATEGORY

ALL_CELLS = {(i, u) for i in Level for u in Level}
LEVEL_ORDER = {Level.HIGH: 0, Level.LOW: 1}


class ConfigRuleError(Exception):
    """설정 규칙 위반 (중복·없는 id·참조 중인 건물 삭제 등) → 409."""


def _no_duplicates(values: Sequence[object], what: str) -> None:
    seen: set[object] = set()
    for v in values:
        if v in seen:
            raise ConfigRuleError(f"{what} '{v}'이(가) 두 번 들어 있어요.")
        seen.add(v)


# ── 카테고리 ──
def list_categories(db: Session) -> list[CategoryItem]:
    rows = db.scalars(select(Category).order_by(Category.name)).all()
    return [CategoryItem(id=c.id, name=c.name, is_active=c.is_active) for c in rows]


def save_categories(db: Session, items: list[CategoryIn]) -> None:
    _no_duplicates([i.name for i in items], "카테고리 이름")
    _no_duplicates([i.id for i in items if i.id], "카테고리 id")
    current = {c.id: c for c in db.scalars(select(Category).with_for_update()).all()}
    given = {i.id for i in items if i.id}
    if unknown := given - current.keys():
        raise ConfigRuleError(f"없는 카테고리 id가 있어요: {', '.join(map(str, unknown))}")
    if missing := [c.name for cid, c in current.items() if cid not in given]:
        raise ConfigRuleError(
            f"카테고리는 지울 수 없어요(접수된 신고가 참조). 대신 끄기(is_active: false)로 바꿔 주세요: {', '.join(missing)}"
        )
    for i in items:
        if i.id and current[i.id].name == DEFAULT_CATEGORY and (i.name != DEFAULT_CATEGORY or not i.is_active):
            raise ConfigRuleError(f"기본 카테고리 '{DEFAULT_CATEGORY}'는 끄거나 이름을 바꿀 수 없어요(분류가 안 될 때 쓰는 값).")
    if not any(i.is_active for i in items):
        raise ConfigRuleError("켜진 카테고리가 하나 이상 있어야 해요.")
    # 두 카테고리의 이름을 맞바꾸는 경우 UNIQUE에 걸리지 않게 바꿀 이름을 잠시 비켜 둠 (건물과 같은 방식)
    for i in items:
        if i.id and current[i.id].name != i.name:
            current[i.id].name = f"__renaming__{i.id}"[:50]
    db.flush()
    for i in items:
        if i.id:
            current[i.id].name, current[i.id].is_active = i.name, i.is_active
        else:
            db.add(Category(name=i.name, is_active=i.is_active))
    db.flush()


# ── 건물 ──
def _aliases(raw: list[str]) -> list[str]:
    return list(dict.fromkeys(raw))  # 순서 유지하며 중복 제거


def list_buildings(db: Session) -> list[BuildingItem]:
    rows = db.scalars(select(Building).order_by(Building.name)).all()
    return [BuildingItem(id=b.id, name=b.name, aliases=list(b.aliases or [])) for b in rows]


def _in_use(db: Session, building_id: uuid.UUID) -> bool:
    return bool(
        db.scalar(select(exists().where(Report.building_id == building_id)))
        or db.scalar(select(exists().where(ProblemCluster.building_id == building_id)))
        or db.scalar(select(exists().where(PredictionStat.building_id == building_id)))
    )


def save_buildings(db: Session, items: list[BuildingIn]) -> None:
    _no_duplicates([i.name for i in items], "건물 이름")
    _no_duplicates([i.id for i in items if i.id], "건물 id")
    current = {b.id: b for b in db.scalars(select(Building).with_for_update()).all()}
    given = {i.id for i in items if i.id}
    if unknown := given - current.keys():
        raise ConfigRuleError(f"없는 건물 id가 있어요: {', '.join(map(str, unknown))}")
    removed = [b for bid, b in current.items() if bid not in given]
    if in_use := [b.name for b in removed if _in_use(db, b.id)]:
        raise ConfigRuleError(
            f"신고·문제 후보·예측에 쓰인 건물은 지울 수 없어요: {', '.join(in_use)}"
        )
    for b in removed:
        db.delete(b)
    db.flush()  # 지운 이름을 새 건물이 다시 쓸 수 있게 먼저 반영
    # 두 건물의 이름을 맞바꾸는 경우 UNIQUE에 걸리지 않게 바꿀 이름을 잠시 비켜 둠
    renamed = [i for i in items if i.id and current[i.id].name != i.name]
    for i in renamed:
        assert i.id is not None
        current[i.id].name = f"__renaming__{i.id}"
    db.flush()
    for i in items:
        if i.id:
            current[i.id].name, current[i.id].aliases = i.name, _aliases(i.aliases)
        else:
            db.add(Building(name=i.name, aliases=_aliases(i.aliases)))
    db.flush()


# ── 우선순위 매트릭스 ──
def get_priority_matrix(db: Session) -> list[PriorityRuleItem]:
    rows = db.scalars(select(PriorityMatrixRule)).all()
    items = [PriorityRuleItem(impact=r.impact, urgency=r.urgency, resulting_priority=r.resulting_priority) for r in rows]
    return sorted(items, key=lambda r: (LEVEL_ORDER[r.impact], LEVEL_ORDER[r.urgency]))


def save_priority_matrix(db: Session, items: list[PriorityRuleItem]) -> None:
    cells = [(i.impact, i.urgency) for i in items]
    if set(cells) != ALL_CELLS or len(cells) != len(ALL_CELLS):
        raise ConfigRuleError("영향도·긴급도 고/저 4칸(고고·고저·저고·저저)이 정확히 한 번씩 있어야 해요.")
    current = {(r.impact, r.urgency): r for r in db.scalars(select(PriorityMatrixRule).with_for_update()).all()}
    for i in items:
        row = current.get((i.impact, i.urgency))
        if row is None:
            db.add(PriorityMatrixRule(impact=i.impact, urgency=i.urgency, resulting_priority=i.resulting_priority))
        else:
            row.resulting_priority = i.resulting_priority
    db.flush()


# ── SLA ──
def _blank_to_none(v: str | None) -> str | None:
    return v or None


def get_sla(db: Session) -> list[SlaItem]:
    rows = db.scalars(select(SlaConfig).order_by(SlaConfig.priority)).all()
    return [
        SlaItem(
            priority=r.priority,
            sla_hours=r.sla_hours,
            escalation_50pct_action=r.escalation_50pct_action,
            escalation_100pct_action=r.escalation_100pct_action,
            escalation_150pct_action=r.escalation_150pct_action,
        )
        for r in rows
    ]


def save_sla(db: Session, items: list[SlaItem]) -> None:
    """이미 접수된 신고의 sla_deadline은 다시 계산하지 않음 — 새 신고부터 적용 (재오픈과 같은 원칙)."""
    prios = [i.priority for i in items]
    if set(prios) != set(Priority) or len(prios) != len(Priority):
        raise ConfigRuleError("P1~P4가 정확히 한 번씩 있어야 해요.")
    current = {r.priority: r for r in db.scalars(select(SlaConfig).with_for_update()).all()}
    for i in items:
        row = current.get(i.priority)
        if row is None:
            row = SlaConfig(priority=i.priority, sla_hours=i.sla_hours)
            db.add(row)
        row.sla_hours = i.sla_hours
        row.escalation_50pct_action = _blank_to_none(i.escalation_50pct_action)
        row.escalation_100pct_action = _blank_to_none(i.escalation_100pct_action)
        row.escalation_150pct_action = _blank_to_none(i.escalation_150pct_action)
    db.flush()


# ── 탐지 임계치 ── (행 하나만 씀 — 여러 개면 가장 먼저 찾은 것)
def _detection_row(db: Session, lock: bool = False) -> DetectionConfig | None:
    stmt = select(DetectionConfig).order_by(DetectionConfig.id).limit(1)
    return db.scalar(stmt.with_for_update() if lock else stmt)


def get_detection(db: Session) -> DetectionSettings:
    row = _detection_row(db)
    if row is None:  # 0002 시드로 항상 있어야 하지만, 없으면 명세 기본값
        return DetectionSettings(threshold_count=3, threshold_hours=72)
    return DetectionSettings(threshold_count=row.threshold_count, threshold_hours=row.threshold_hours)


def save_detection(db: Session, body: DetectionSettings) -> None:
    row = _detection_row(db, lock=True)
    if row is None:
        db.add(DetectionConfig(threshold_count=body.threshold_count, threshold_hours=body.threshold_hours))
    else:
        row.threshold_count, row.threshold_hours = body.threshold_count, body.threshold_hours
    db.flush()
