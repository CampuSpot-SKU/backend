"""상세보기 유사 사례 검색 — 비슷한 과거 해결 건과 처리 메모 (작업 1-8c, 명세서 3-3·4-2).

- 기준: 이 신고의 설명 임베딩(reports.embedding, 1-8b 탐지 배치가 매일 채움)과 코사인 거리가 가까운 신고.
- 대상: **같은 카테고리**의 **해결·종료된** 다른 신고(건물은 달라도 됨 — 다른 건물에서 같은 고장을 어떻게
  처리했는지가 처리 메모로 쓸모 있음). 거리 SIMILAR_MAX_DISTANCE 이하만, 가까운 순.
- 처리 메모: 그 신고가 마지막으로 '해결'로 바뀔 때 남긴 메모(없으면 null).
- 이 신고의 임베딩이 아직 없으면(접수 후 다음 탐지 배치 전) embedding_ready=false·빈 목록.
"""
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.config import Building, Category
from app.models.enums import ReportStatus
from app.models.report import Report, ReportStatusHistory
from app.schemas.admin import NamedRef, SimilarReport, SimilarReportList

DONE_STATUSES = (ReportStatus.RESOLVED, ReportStatus.CLOSED)
# 코사인 거리(0 = 같은 뜻). 탐지 묶기(같은 문제, 0.05)보다 넓게 — "비슷한 종류의 고장"까지
SIMILAR_MAX_DISTANCE = 0.3


def find_similar(db: Session, report_id: uuid.UUID, limit: int = 5) -> SimilarReportList | None:
    """None = 신고 없음(404)."""
    target = db.execute(
        select(Report.category_id, Report.embedding).where(Report.id == report_id)
    ).one_or_none()
    if target is None:
        return None
    category_id, embedding = target
    if embedding is None:
        return SimilarReportList(embedding_ready=False, items=[])

    distance = Report.embedding.cosine_distance(embedding).label("distance")
    rows = db.execute(
        select(Report, Category, Building, distance)
        .join(Category, Report.category_id == Category.id)
        .outerjoin(Building, Report.building_id == Building.id)
        .where(
            Report.id != report_id,
            Report.category_id == category_id,
            Report.status.in_(DONE_STATUSES),
            Report.embedding.is_not(None),
            distance <= SIMILAR_MAX_DISTANCE,
        )
        .order_by(distance)
        .limit(limit)
    ).all()
    resolved = _last_resolution(db, [r.id for r, *_ in rows])
    items = [
        SimilarReport(
            id=r.id,
            display_no=r.display_no,
            category=NamedRef(id=c.id, name=c.name),
            building=NamedRef(id=b.id, name=b.name) if b else None,
            floor=r.floor,
            detail=r.detail,
            location_raw=r.location_raw,
            description=r.description,
            status=r.status,
            created_at=r.created_at,
            resolved_at=resolved[r.id][0] if r.id in resolved else None,
            resolution_memo=resolved[r.id][1] if r.id in resolved else None,
            distance=round(float(d), 3),
        )
        for r, c, b, d in rows
    ]
    return SimilarReportList(embedding_ready=True, items=items)


def _last_resolution(db: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[datetime, str | None]]:
    """신고 id → (마지막 '해결' 전이 시각, 그때 메모)."""
    if not ids:
        return {}
    rows = db.execute(
        select(ReportStatusHistory.report_id, ReportStatusHistory.changed_at, ReportStatusHistory.memo)
        .where(ReportStatusHistory.report_id.in_(ids), ReportStatusHistory.to_status == ReportStatus.RESOLVED)
        .order_by(ReportStatusHistory.changed_at)
    ).all()
    return {rid: (at, memo) for rid, at, memo in rows}  # 시각순이라 마지막 것이 남음
