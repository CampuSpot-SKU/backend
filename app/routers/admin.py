"""관리자용 엔드포인트 — 로그인, 신고 목록·상세·상태 변경 (작업 1-6, 명세서 5-1).

로그인 외 모든 엔드포인트는 Authorization: Bearer {access_token} 필요 (CurrentAdmin 의존성).
탐지·예측(/admin/problem-clusters, /admin/predictions), 설정(/admin/config/*)은 아직 — 1-8 이후.
"""
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from app.deps import CurrentAdmin, DbSession
from app.models.enums import Priority, ReportStatus
from app.models.report import Report
from app.schemas.admin import (
    AdminReportDetail,
    AdminReportList,
    LoginIn,
    SlaStatus,
    SortKey,
    StatusChangeIn,
    TokenOut,
)
from app.services.auth import (
    NOT_CONFIGURED,
    AuthNotConfiguredError,
    authenticate,
    create_access_token,
)
from app.services.photo_storage import PhotoStorage, get_photo_storage
from app.services.report_query import get_report_detail, list_reports
from app.services.report_workflow import InvalidTransitionError, change_status

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

PhotoStorageDep = Annotated[PhotoStorage, Depends(get_photo_storage)]

_AUTH_ERRORS: dict[int | str, dict[str, Any]] = {401: {"description": "토큰 없음·만료·위조"}, 503: {"description": "JWT_SECRET 미설정"}}


@router.post(
    "/auth/login",
    response_model=TokenOut,
    responses={401: {"description": "아이디 또는 비밀번호 틀림"}, 503: {"description": "JWT_SECRET 미설정"}},
)
def login(body: LoginIn, db: DbSession) -> TokenOut:
    """관리자 로그인 → JWT 발급. 이후 요청에 `Authorization: Bearer {access_token}` 헤더로 사용."""
    admin = authenticate(db, body.login_id, body.password)
    if admin is None:
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않아요.")
    try:
        token = create_access_token(admin.id)
    except AuthNotConfiguredError:
        raise NOT_CONFIGURED from None
    logger.info("관리자 로그인 %s", admin.login_id)
    return TokenOut(access_token=token)


@router.get("/reports", response_model=AdminReportList, responses=_AUTH_ERRORS)
def get_reports(
    db: DbSession,
    _admin: CurrentAdmin,
    status: ReportStatus | None = None,
    category_id: uuid.UUID | None = None,
    priority: Priority | None = None,
    sla_status: SlaStatus | None = None,
    sort: SortKey = "-created_at",
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminReportList:
    """접수 목록(큐). 필터는 모두 선택. sort: -created_at(최신순, 기본) / created_at / sla_deadline(마감 급한 순) / priority(P1부터)."""
    return list_reports(
        db,
        status=status,
        category_id=category_id,
        priority=priority,
        sla_status=sla_status,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/reports/{report_id}",
    response_model=AdminReportDetail,
    responses={**_AUTH_ERRORS, 404: {"description": "신고 없음"}},
)
def get_report(
    report_id: uuid.UUID, db: DbSession, _admin: CurrentAdmin, photos: PhotoStorageDep
) -> AdminReportDetail:
    """상세보기 — 신고 전체 필드 + 상태 이력 타임라인. photo_url은 10분짜리 임시 링크(없거나 실패면 null)."""
    detail = get_report_detail(db, report_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="신고를 찾을 수 없어요.")
    return _with_photo_link(detail, photos)


def _with_photo_link(detail: AdminReportDetail, photos: PhotoStorage) -> AdminReportDetail:
    """DB에는 저장소 경로(reports/{id}/photo)가 있음 → 비공개 버킷이라 볼 때마다 임시 링크로 바꿔 내려줌 (1-10)."""
    if not detail.photo_url:
        return detail
    return detail.model_copy(update={"photo_url": photos.signed_url(detail.photo_url)})


@router.patch(
    "/reports/{report_id}/status",
    response_model=AdminReportDetail,
    responses={**_AUTH_ERRORS, 404: {"description": "신고 없음"}, 409: {"description": "허용되지 않는 상태 변경"}},
)
def patch_report_status(
    report_id: uuid.UUID, body: StatusChangeIn, db: DbSession, admin: CurrentAdmin,
    photos: PhotoStorageDep,
) -> AdminReportDetail:
    """상태 변경 + 이력 기록(변경한 관리자·메모). 응답은 변경 후 상세."""
    # 두 관리자가 동시에 바꿔도 이력이 꼬이지 않게 행 잠금
    report = db.scalar(select(Report).where(Report.id == report_id).with_for_update())
    if report is None:
        raise HTTPException(status_code=404, detail="신고를 찾을 수 없어요.")
    try:
        change_status(db, report, body.to_status, body.memo, admin.id)
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    db.commit()
    logger.info("신고 상태 변경 display_no=%s → %s by %s", report.display_no, body.to_status.value, admin.login_id)
    detail = get_report_detail(db, report_id)
    assert detail is not None
    # 화면은 이 응답으로 상세를 갈아끼우므로 사진도 상세 조회와 똑같이 임시 링크로
    return _with_photo_link(detail, photos)
