"""관리자용 탐지·예측 — 문제 후보 목록·승격/기각, 재발 예측 목록 (작업 1-8, 명세서 3-3·5-1).

데이터는 탐지·예측 배치(ai 서비스)가 problem_clusters·prediction_stats에 채운다. 이 라우터는
읽기와 승격/기각만. 인증은 다른 관리자 API와 같음(Authorization: Bearer, CurrentAdmin).
응답 모양은 frontend 화면 계약(team-docs briefs/1-8.md)과 같다.
"""
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.deps import CurrentAdmin, DbSession
from app.models.enums import ClusterStatus
from app.models.problem import ProblemCluster
from app.schemas.admin import (
    ClusterStatusChangeIn,
    PredictionList,
    ProblemClusterItem,
    ProblemClusterList,
)
from app.services.detection_query import (
    ClusterAlreadyDecidedError,
    decide_cluster,
    get_cluster,
    list_clusters,
    list_predictions,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-detection"])

_AUTH_ERRORS: dict[int | str, dict[str, Any]] = {401: {"description": "토큰 없음·만료·위조"}, 503: {"description": "JWT_SECRET 미설정"}}


@router.get("/problem-clusters", response_model=ProblemClusterList, responses=_AUTH_ERRORS)
def get_problem_clusters(
    db: DbSession, _admin: CurrentAdmin, status: ClusterStatus | None = None
) -> ProblemClusterList:
    """탐지된 문제 후보 목록 — 감지 시각 최신순. status(후보·승격·기각)가 없으면 전체."""
    return ProblemClusterList(items=list_clusters(db, status=status))


@router.patch(
    "/problem-clusters/{cluster_id}",
    response_model=ProblemClusterItem,
    responses={**_AUTH_ERRORS, 404: {"description": "문제 후보 없음"}, 409: {"description": "이미 승격·기각된 후보"}},
)
def patch_problem_cluster(
    cluster_id: uuid.UUID, body: ClusterStatusChangeIn, db: DbSession, admin: CurrentAdmin
) -> ProblemClusterItem:
    """후보 → 승격 또는 기각 (후보 상태일 때 한 번만). 응답은 바뀐 후보 한 건 (목록 원소와 같은 모양)."""
    # 두 관리자가 동시에 눌러도 한 번만 결정되게 행 잠금
    cluster = db.scalar(select(ProblemCluster).where(ProblemCluster.id == cluster_id).with_for_update())
    if cluster is None:
        raise HTTPException(status_code=404, detail="문제 후보를 찾을 수 없어요.")
    try:
        decide_cluster(db, cluster, ClusterStatus(body.status))
    except ClusterAlreadyDecidedError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    db.commit()
    logger.info("문제 후보 %s → %s by %s", cluster_id, body.status, admin.login_id)
    item = get_cluster(db, cluster_id)
    assert item is not None
    return item


@router.get("/predictions", response_model=PredictionList, responses=_AUTH_ERRORS)
def get_predictions(db: DbSession, _admin: CurrentAdmin) -> PredictionList:
    """재발 예측 목록 — 다음 예상 시점이 빠른 순. 비어 있으면 '예측할 만큼 신고가 없음'(명세 3-3, 3건 미만)."""
    return PredictionList(items=list_predictions(db))
