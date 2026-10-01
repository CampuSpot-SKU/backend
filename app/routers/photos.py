"""신고 사진 업로드 — 접수 전에 올려 두면 접수될 때 그 신고에 붙는다 (작업 1-10, 명세서 5-1·11장).

엔드포인트 (계약 — 경로·필드·문구 임의 변경 금지):
  POST   /chat/sessions/{session_id}/photo   multipart 필드 `file` 1개 → 201 {attached, content_type, size_bytes}
  DELETE /chat/sessions/{session_id}/photo   → 204 (올린 사진이 없어도 204)
검사는 파일 첫 바이트·실제 크기로 한다(브라우저가 보낸 Content-Type·파일 이름 무시). 저장 로직은
app/services/photo_storage.py. 접수 때 붙이기는 routers/chat.py `_create`, 관리자 임시 링크는 routers/admin.py.
"""
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import DbSession
from app.models.chat import ChatSession
from app.rate_limit import limiter
from app.services.photo_storage import (
    MAX_BYTES,
    PhotoStorage,
    PhotoTooLargeError,
    StorageError,
    UnsupportedPhotoError,
    get_photo_storage,
    validate_image,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])

PHOTO_RATE_LIMIT = "10/minute"
NO_SESSION = "대화 세션을 찾을 수 없어요."
TOO_LARGE = "사진은 5MB 이하만 올릴 수 있어요."
UNSUPPORTED = "jpg 또는 png 사진만 올릴 수 있어요."
STORAGE_FAILED = "사진을 저장하지 못했어요. 잠시 후 다시 시도해 주세요."

Storage = Annotated[PhotoStorage, Depends(get_photo_storage)]


class PhotoAttached(BaseModel):
    attached: bool = True
    content_type: str
    size_bytes: int


def _require_session(db: Session, session_id: uuid.UUID) -> None:
    if db.get(ChatSession, session_id) is None:
        raise HTTPException(status_code=404, detail=NO_SESSION)


@router.post(
    "/sessions/{session_id}/photo",
    response_model=PhotoAttached,
    status_code=201,
    responses={
        404: {"description": NO_SESSION},
        413: {"description": TOO_LARGE},
        415: {"description": UNSUPPORTED},
        429: {"description": "세션당 요청 한도 초과"},
        503: {"description": STORAGE_FAILED},
    },
)
@limiter.limit(PHOTO_RATE_LIMIT)
def upload_photo(
    request: Request,  # slowapi가 요구 (레이트 리밋 키 계산용)
    session_id: uuid.UUID,
    file: Annotated[UploadFile, File()],
    db: DbSession,
    storage: Storage,
) -> PhotoAttached:
    """접수 전 사진 1장 — 세션당 대기 사진은 1장이라 다시 올리면 교체된다."""
    _require_session(db, session_id)
    data = file.file.read(MAX_BYTES + 1)  # 한도+1바이트까지만 읽음 → 전체를 메모리에 올리지 않음
    try:
        content_type = validate_image(data)
    except PhotoTooLargeError:
        raise HTTPException(status_code=413, detail=TOO_LARGE) from None
    except UnsupportedPhotoError:
        raise HTTPException(status_code=415, detail=UNSUPPORTED) from None
    try:
        storage.upload_pending(session_id, data, content_type)
    except StorageError as e:
        logger.warning("사진 업로드 실패 session=%s: %s", session_id, e)
        raise HTTPException(status_code=503, detail=STORAGE_FAILED) from None
    return PhotoAttached(content_type=content_type, size_bytes=len(data))


@router.delete(
    "/sessions/{session_id}/photo",
    status_code=204,
    responses={404: {"description": NO_SESSION}, 429: {"description": "세션당 요청 한도 초과"},
               503: {"description": STORAGE_FAILED}},
)
@limiter.limit(PHOTO_RATE_LIMIT)
def delete_photo(
    request: Request,  # slowapi가 요구 (레이트 리밋 키 계산용)
    session_id: uuid.UUID,
    db: DbSession,
    storage: Storage,
) -> Response:
    """대기 사진 삭제 (화면 칩의 [삭제]). 올린 사진이 없어도 204."""
    _require_session(db, session_id)
    try:
        storage.delete_pending(session_id)
    except StorageError as e:
        logger.warning("사진 삭제 실패 session=%s: %s", session_id, e)
        raise HTTPException(status_code=503, detail=STORAGE_FAILED) from None
    return Response(status_code=204)
