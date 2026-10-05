"""관리자 통계 — 카테고리별 건수·평균 처리시간·SLA 준수율 (작업 1-16, 명세서 4-2).

인증은 다른 관리자 API와 같음(Authorization: Bearer, CurrentAdmin). 읽기 전용.
계산 기준은 services/stats_query.py 맨 위 주석.
"""
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.deps import CurrentAdmin, DbSession
from app.schemas.stats import AdminStats
from app.services.stats_query import get_stats

router = APIRouter(prefix="/admin", tags=["admin-stats"])

_AUTH_ERRORS: dict[int | str, dict[str, Any]] = {401: {"description": "토큰 없음·만료·위조"}, 503: {"description": "JWT_SECRET 미설정"}}


@router.get("/stats", response_model=AdminStats, responses=_AUTH_ERRORS)
def get_admin_stats(
    db: DbSession,
    _admin: CurrentAdmin,
    days: Annotated[int | None, Query(ge=1, le=365, description="최근 N일 접수분만. 없으면 전체 기간")] = None,
) -> AdminStats:
    """카테고리별·우선순위별·상태별 건수 + 평균 처리시간 + SLA 준수율."""
    return get_stats(db, days=days)
