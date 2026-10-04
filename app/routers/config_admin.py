"""관리자 설정 관리 — 카테고리·건물·우선순위 매트릭스·SLA·탐지 임계치 (작업 1-17, 명세서 5-1).

모두 GET(조회) / PUT(전체를 통째로 보내 저장). 응답은 저장 후 GET과 같은 모양.
잘못된 형식은 422(FastAPI 검사), 규칙 위반(중복·없는 id·참조 중인 건물 삭제·기본 카테고리 끄기)은 409.
새 신고는 접수 때마다 이 설정을 읽으므로 저장하면 다음 신고부터 반영 — 이미 접수된 신고는 그대로.
"""
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.deps import CurrentAdmin, DbSession
from app.schemas.config import (
    BuildingList,
    BuildingListIn,
    CategoryList,
    CategoryListIn,
    DetectionSettings,
    PriorityMatrix,
    PriorityMatrixIn,
    SlaList,
    SlaListIn,
)
from app.services import config_admin as svc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/config", tags=["admin-config"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"description": "토큰 없음·만료·위조"},
    503: {"description": "JWT_SECRET 미설정"},
}
_PUT_ERRORS: dict[int | str, dict[str, Any]] = {**_ERRORS, 409: {"description": "설정 규칙 위반 — 아무것도 저장 안 됨"}}


def _save(db: Session, what: str, admin_login: str, fn: Callable[[], None]) -> None:
    """검사·저장을 한 트랜잭션으로 — 하나라도 어기면 전부 되돌리고 409."""
    try:
        fn()
        db.commit()
    except svc.ConfigRuleError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(e)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="이름이 겹치거나 다른 데이터와 맞지 않아 저장하지 못했어요.") from None
    logger.info("설정 변경 %s by %s", what, admin_login)


@router.get("/categories", response_model=CategoryList, responses=_ERRORS)
def get_categories(db: DbSession, _admin: CurrentAdmin) -> CategoryList:
    """카테고리 목록(꺼진 것 포함, 이름순). 꺼진 카테고리는 새 신고 분류에 쓰이지 않음."""
    return CategoryList(items=svc.list_categories(db))


@router.put("/categories", response_model=CategoryList, responses=_PUT_ERRORS)
def put_categories(body: CategoryListIn, db: DbSession, admin: CurrentAdmin) -> CategoryList:
    """전체 목록 저장 — id 있으면 수정, 없으면 추가. 삭제는 없음(끄기로). 기존 항목이 빠지면 409."""
    _save(db, "categories", admin.login_id, lambda: svc.save_categories(db, body.items))
    return CategoryList(items=svc.list_categories(db))


@router.get("/buildings", response_model=BuildingList, responses=_ERRORS)
def get_buildings(db: DbSession, _admin: CurrentAdmin) -> BuildingList:
    """건물 목록(이름순) + 별칭(챗봇 대화에서 다르게 부르는 이름)."""
    return BuildingList(items=svc.list_buildings(db))


@router.put("/buildings", response_model=BuildingList, responses=_PUT_ERRORS)
def put_buildings(body: BuildingListIn, db: DbSession, admin: CurrentAdmin) -> BuildingList:
    """전체 목록 저장 — id 있으면 수정, 없으면 추가, 빠진 건물은 삭제(신고 등이 참조하면 409)."""
    _save(db, "buildings", admin.login_id, lambda: svc.save_buildings(db, body.items))
    return BuildingList(items=svc.list_buildings(db))


@router.get("/priority-matrix", response_model=PriorityMatrix, responses=_ERRORS)
def get_priority_matrix(db: DbSession, _admin: CurrentAdmin) -> PriorityMatrix:
    """영향도×긴급도 → 우선순위 4칸 (고고·고저·저고·저저 순)."""
    return PriorityMatrix(items=svc.get_priority_matrix(db))


@router.put("/priority-matrix", response_model=PriorityMatrix, responses=_PUT_ERRORS)
def put_priority_matrix(body: PriorityMatrixIn, db: DbSession, admin: CurrentAdmin) -> PriorityMatrix:
    """4칸 전체 저장 (4칸이 정확히 한 번씩). 새 신고부터 적용."""
    _save(db, "priority-matrix", admin.login_id, lambda: svc.save_priority_matrix(db, body.items))
    return PriorityMatrix(items=svc.get_priority_matrix(db))


@router.get("/sla", response_model=SlaList, responses=_ERRORS)
def get_sla(db: DbSession, _admin: CurrentAdmin) -> SlaList:
    """P1~P4 SLA 목표시간(시간)과 에스컬레이션 단계별 조치 문구."""
    return SlaList(items=svc.get_sla(db))


@router.put("/sla", response_model=SlaList, responses=_PUT_ERRORS)
def put_sla(body: SlaListIn, db: DbSession, admin: CurrentAdmin) -> SlaList:
    """P1~P4 전체 저장. 이미 접수된 신고의 마감은 다시 계산하지 않음(새 신고부터)."""
    _save(db, "sla", admin.login_id, lambda: svc.save_sla(db, body.items))
    return SlaList(items=svc.get_sla(db))


@router.get("/detection", response_model=DetectionSettings, responses=_ERRORS)
def get_detection(db: DbSession, _admin: CurrentAdmin) -> DetectionSettings:
    """탐지 임계치 — threshold_hours 시간 안에 같은 곳 신고가 threshold_count건 이상이면 문제 후보."""
    return svc.get_detection(db)


@router.put("/detection", response_model=DetectionSettings, responses=_PUT_ERRORS)
def put_detection(body: DetectionSettings, db: DbSession, admin: CurrentAdmin) -> DetectionSettings:
    """탐지 임계치 저장. 탐지 배치(ai)가 다음 실행 때 이 값을 읽음."""
    _save(db, "detection", admin.login_id, lambda: svc.save_detection(db, body))
    return svc.get_detection(db)
