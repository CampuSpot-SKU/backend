"""관리자용 탐지·예측 조회와 승격/기각 (작업 1-8 — 명세서 3-3, 5-1).

problem_clusters·prediction_stats는 탐지·예측 배치(차원, ai)가 채우고, 여기서는 읽기와
문제 후보의 승격/기각만 한다. 응답 모양은 접수 목록과 같은 규칙(FK는 {id, name} 중첩 객체).
"""
import uuid

from sqlalchemy import ScalarSelect, func, select
from sqlalchemy.orm import Session

from app.models.config import Building, Category
from app.models.enums import ClusterStatus
from app.models.problem import PredictionStat, ProblemCluster, problem_cluster_reports
from app.schemas.admin import NamedRef, PredictionItem, ProblemClusterItem


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


def list_clusters(
    db: Session, status: ClusterStatus | None = None, cluster_id: uuid.UUID | None = None
) -> list[ProblemClusterItem]:
    """문제 후보 목록 — 감지 시각 최신순. status가 없으면 전체."""
    stmt = (
        select(ProblemCluster, Category, Building, _report_count().label("report_count"))
        .join(Category, Category.id == ProblemCluster.category_id)
        .outerjoin(Building, Building.id == ProblemCluster.building_id)
        .order_by(ProblemCluster.detected_at.desc(), ProblemCluster.id)
    )
    if status is not None:
        stmt = stmt.where(ProblemCluster.status == status)
    if cluster_id is not None:
        stmt = stmt.where(ProblemCluster.id == cluster_id)
    items = []
    for cluster, category, building, count in db.execute(stmt).all():
        category_ref = _ref(category)
        assert category_ref is not None  # category_id는 NOT NULL + 내부 조인
        items.append(
            ProblemClusterItem(
                id=cluster.id,
                building=_ref(building),
                detail=cluster.detail,
                category=category_ref,
                report_count=count,
                detected_at=cluster.detected_at,
                status=cluster.status,
            )
        )
    return items


def get_cluster(db: Session, cluster_id: uuid.UUID) -> ProblemClusterItem | None:
    found = list_clusters(db, cluster_id=cluster_id)
    return found[0] if found else None


def decide_cluster(db: Session, cluster: ProblemCluster, to_status: ClusterStatus) -> None:
    """후보 → 승격 또는 기각 (한 번만). 이미 결정된 후보는 바꾸지 않음 — 커밋은 호출한 쪽."""
    if cluster.status != ClusterStatus.CANDIDATE:
        raise ClusterAlreadyDecidedError(
            f"이미 '{cluster.status.value}' 처리된 문제 후보예요. 후보 상태일 때만 승격·기각할 수 있어요."
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
            )
        )
    return items
