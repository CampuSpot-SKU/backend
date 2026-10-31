"""관리자용 탐지·예측 조회와 승격/기각 (작업 1-8 — 명세서 3-3, 5-1).

problem_clusters·prediction_stats는 탐지·예측 배치(차원, ai)가 채우고, 여기서는 읽기와
반복 신고 묶음의 승격/기각과 일괄 처리만 한다. 응답 모양은 접수 목록과 같은 규칙(FK는 {id, name} 중첩 객체).
"""
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, ScalarSelect, func, select
from sqlalchemy.orm import Session

from app.models.config import Building, Category
from app.models.enums import ClusterStatus, ReportStatus
from app.models.problem import PredictionStat, ProblemCluster, problem_cluster_reports
from app.models.report import Report
from app.schemas.admin import (
    NamedRef,
    PredictionItem,
    ProblemClusterDetail,
    ProblemClusterItem,
)
from app.services.report_query import OPEN_STATUSES, cluster_member_reports
from app.services.report_workflow import change_status


class ClusterAlreadyDecidedError(Exception):
    """이미 승격·기각된 후보를 다시 바꾸려 함 → 409."""


def _ref(row: Building | Category | None) -> NamedRef | None:
    return NamedRef(id=row.id, name=row.name) if row is not None else None


def _report_count() -> ScalarSelect[int]:
    """후보 한 건에 묶인 신고 수 (problem_cluster_reports 행 수)."""
    return (
        select(func.count())
        .select_from(problem_cluster_reports)
        .where(problem_cluster_reports.c.cluster_id == ProblemCluster.id)
        .correlate(ProblemCluster)
        .scalar_subquery()
    )


def _member_stat(expr: ColumnElement[Any]) -> ScalarSelect[Any]:
    """묶음에 묶인 신고들에 대한 집계 한 값 (건수·처음/마지막 접수 시각)."""
    return (
        select(expr)
        .select_from(
            problem_cluster_reports.join(Report, Report.id == problem_cluster_reports.c.report_id)
        )
        .where(problem_cluster_reports.c.cluster_id == ProblemCluster.id)
        .correlate(ProblemCluster)
        .scalar_subquery()
    )


def list_clusters(
    db: Session, status: ClusterStatus | None = None, cluster_id: uuid.UUID | None = None
) -> list[ProblemClusterItem]:
    """반복 신고 묶음 목록 — 감지 시각 최신순. status가 없으면 전체."""
    stmt = (
        select(
            ProblemCluster,
            Category,
            Building,
            _report_count().label("report_count"),
            _member_stat(func.count().filter(Report.status.in_(OPEN_STATUSES))).label("open_count"),
            _member_stat(func.min(Report.created_at)).label("first_at"),
            _member_stat(func.max(Report.created_at)).label("last_at"),
        )
        .join(Category, Category.id == ProblemCluster.category_id)
        .outerjoin(Building, Building.id == ProblemCluster.building_id)
        .order_by(ProblemCluster.detected_at.desc(), ProblemCluster.id)
    )
    if status is not None:
        stmt = stmt.where(ProblemCluster.status == status)
    if cluster_id is not None:
        stmt = stmt.where(ProblemCluster.id == cluster_id)
    items = []
    for cluster, category, building, count, open_count, first_at, last_at in db.execute(stmt).all():
        category_ref = _ref(category)
        assert category_ref is not None  # category_id는 NOT NULL + 내부 조인
        items.append(
            ProblemClusterItem(
                id=cluster.id,
                building=_ref(building),
                detail=cluster.detail,
                category=category_ref,
                report_count=count,
                open_count=open_count or 0,
                first_reported_at=first_at,
                last_reported_at=last_at,
                detected_at=cluster.detected_at,
                status=cluster.status,
            )
        )
    return items


def get_cluster(db: Session, cluster_id: uuid.UUID) -> ProblemClusterItem | None:
    found = list_clusters(db, cluster_id=cluster_id)
    return found[0] if found else None


def get_cluster_detail(db: Session, cluster_id: uuid.UUID) -> ProblemClusterDetail | None:
    item = get_cluster(db, cluster_id)
    if item is None:
        return None
    return ProblemClusterDetail(**item.model_dump(), reports=cluster_member_reports(db, cluster_id))


class ClusterRejectedError(Exception):
    """'문제 아님'으로 정리한 묶음은 일괄 처리할 수 없음 → 409."""


_PATH = [
    ReportStatus.RECEIVED,
    ReportStatus.ASSIGNED,
    ReportStatus.IN_PROGRESS,
    ReportStatus.RESOLVED,
    ReportStatus.CLOSED,
]
BULK_MEMO = "반복 신고 일괄 처리"


def advance_cluster_reports(
    db: Session,
    cluster: ProblemCluster,
    to_status: ReportStatus,
    memo: str | None,
    admin_id: uuid.UUID | None,
) -> int:
    """묶음에 묶인 신고를 to_status까지 한 단계씩 올린다. 이미 그 단계 이상인 신고는 건드리지 않음.
    상태가 바뀐 신고 수를 돌려준다. 커밋은 호출한 쪽."""
    if cluster.status == ClusterStatus.REJECTED:
        raise ClusterRejectedError("'문제 아님'으로 정리한 묶음은 한 번에 처리할 수 없어요.")
    target = _PATH.index(to_status)
    reports = db.scalars(
        select(Report)
        .join(problem_cluster_reports, problem_cluster_reports.c.report_id == Report.id)
        .where(problem_cluster_reports.c.cluster_id == cluster.id)
        .order_by(Report.id)
        .with_for_update(of=Report)
    ).all()
    changed = 0
    stamp = datetime.now(UTC)  # 한 트랜잭션 안의 여러 단계가 이력에서 순서대로 보이게 1ms씩 늘려 기록
    for r in reports:
        if _PATH.index(r.status) >= target:
            continue
        while _PATH.index(r.status) < target:
            nxt = _PATH[_PATH.index(r.status) + 1]
            last = nxt == to_status
            change_status(db, r, nxt, (memo or BULK_MEMO) if last else BULK_MEMO, admin_id, stamp)
            stamp += timedelta(milliseconds=1)
        changed += 1
    return changed


def decide_cluster(db: Session, cluster: ProblemCluster, to_status: ClusterStatus) -> None:
    """후보 → 승격 또는 기각 (한 번만). 이미 결정된 후보는 바꾸지 않음 — 커밋은 호출한 쪽."""
    if cluster.status != ClusterStatus.CANDIDATE:
        raise ClusterAlreadyDecidedError(
            f"이미 '{cluster.status.value}' 처리된 반복 신고예요. '후보' 상태일 때만 바꿀 수 있어요."
        )
    cluster.status = to_status


def list_predictions(db: Session) -> list[PredictionItem]:
    """재발 예측 목록 — 다음 예상 시점이 빠른 순. 배치가 계산을 마친 행(평균 간격·예상 시점 둘 다 있음)만."""
    stmt = (
        select(PredictionStat, Category, Building)
        .join(Category, Category.id == PredictionStat.category_id)
        .outerjoin(Building, Building.id == PredictionStat.building_id)
        .where(
            PredictionStat.avg_recurrence_days.is_not(None),
            PredictionStat.predicted_next_at.is_not(None),
        )
        .order_by(PredictionStat.predicted_next_at, PredictionStat.id)
    )
    items = []
    for stat, category, building in db.execute(stmt).all():
        category_ref = _ref(category)
        assert category_ref is not None
        assert stat.avg_recurrence_days is not None and stat.predicted_next_at is not None
        items.append(
            PredictionItem(
                building=_ref(building),
                detail=stat.detail,
                category=category_ref,
                avg_recurrence_days=stat.avg_recurrence_days,
                predicted_next_at=stat.predicted_next_at,
                last_occurred_at=stat.last_occurred_at,
            )
        )
    return items
