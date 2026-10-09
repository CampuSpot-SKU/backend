"""신고 사진 저장소 — Supabase Storage 비공개 버킷 `report-photos` (작업 1-10, 명세서 11장·5-1).

흐름 (접수 전 업로드 A안):
  1) 학생이 접수 전에 사진을 올림 → 대기 자리 `pending/{session_id}/photo`에 저장 (세션당 1장, 다시 올리면 덮어씀)
  2) 신고가 접수되는 순간 대기 사진을 `reports/{report_id}/photo`로 옮기고 그 경로를 `reports.photo_url`에 저장
  3) 관리자 상세 조회 때 10분짜리 임시 링크(signed URL)를 만들어 내려줌 — 버킷이 비공개라 영구 주소가 없음

외부 라이브러리 없이 httpx로 Storage REST API를 호출한다. 파일 이름은 항상 `photo`로 고정 —
사용자가 올린 파일 이름·확장자는 어디에도 쓰지 않음(경로 조작 방지). 종류는 저장소 객체의 content-type으로 구분.
서비스 키·임시 링크(토큰)·사용자 파일 이름은 로그·예외 메시지에 넣지 않는다.
"""
import logging
import uuid

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

BUCKET = "report-photos"
MAX_BYTES = 5 * 1024 * 1024  # 5MB (명세서 11장)
SIGNED_URL_SECONDS = 600  # 관리자용 임시 링크 유효 시간 (10분)
UPLOAD_TIMEOUT_SECONDS = 15.0
TIMEOUT_SECONDS = 5.0

JPEG = "image/jpeg"
PNG = "image/png"
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", JPEG),
    (b"\x89PNG\r\n\x1a\n", PNG),
)


class StorageError(Exception):
    """저장소 호출 실패 (설정 누락·네트워크·예상 밖 응답 모두)."""


class PhotoTooLargeError(ValueError):
    """5MB 초과 → 413."""


class UnsupportedPhotoError(ValueError):
    """jpg/png가 아님(빈 파일 포함) → 415."""


def sniff_image_type(data: bytes) -> str | None:
    """파일 첫 바이트(매직 넘버)로 형식 판별 — 브라우저가 보낸 Content-Type·파일 이름은 믿지 않음."""
    for magic, content_type in _MAGIC:
        if data.startswith(magic):
            return content_type
    return None


def validate_image(data: bytes) -> str:
    """크기·형식 검사를 통과하면 content-type을 돌려줌. 크기를 먼저 본다(큰 파일은 형식과 상관없이 413)."""
    if len(data) > MAX_BYTES:
        raise PhotoTooLargeError
    content_type = sniff_image_type(data)
    if content_type is None:
        raise UnsupportedPhotoError
    return content_type


def pending_key(session_id: uuid.UUID) -> str:
    return f"pending/{session_id}/photo"


def report_key(report_id: uuid.UUID) -> str:
    return f"reports/{report_id}/photo"


def _is_not_found(res: httpx.Response) -> bool:
    """Storage는 "객체 없음"을 404 또는 400(본문에 not found)으로 줄 수 있어 둘 다 없음으로 본다."""
    if res.status_code == 404:
        return True
    return res.status_code == 400 and "not found" in res.text.lower()


class PhotoStorage:
    """Supabase Storage REST 호출. 메서드 이름·의미는 차원의 AI 사진 분석(후속)이 그대로 쓰므로 바꾸지 말 것."""

    def __init__(self, url: str, service_key: str) -> None:
        self._url = url.strip().rstrip("/")
        self._key = service_key.strip()

    @property
    def configured(self) -> bool:
        return bool(self._url and self._key)

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "apikey": self._key, **extra}

    def _object_url(self, key: str) -> str:
        return f"{self._url}/storage/v1/object/{BUCKET}/{key}"

    def upload_pending(self, session_id: uuid.UUID, data: bytes, content_type: str) -> None:
        """대기 자리에 저장(덮어씀). 실패·미설정 → StorageError."""
        if not self.configured:
            raise StorageError("photo storage not configured")
        try:
            res = httpx.post(
                self._object_url(pending_key(session_id)),
                content=data,
                headers=self._headers(**{"Content-Type": content_type, "x-upsert": "true"}),
                timeout=UPLOAD_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as e:
            raise StorageError(f"upload failed: {type(e).__name__}") from e
        if res.status_code >= 300:
            raise StorageError(f"upload failed: HTTP {res.status_code}")

    def download_pending(self, session_id: uuid.UUID) -> tuple[bytes, str] | None:
        """대기 사진 내용 — AI 사진 분석(1-10)용. (바이트, 실제 형식) / 사진 없음·미설정 → None / 그 밖의 실패 → StorageError.

        형식은 저장 때 정한 Content-Type이 아니라 첫 바이트로 다시 판별하고, 5MB를 넘거나 jpg/png가 아니면 None."""
        if not self.configured:
            return None
        try:
            res = httpx.get(
                self._object_url(pending_key(session_id)),
                headers=self._headers(),
                timeout=UPLOAD_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as e:
            raise StorageError(f"download failed: {type(e).__name__}") from e
        if _is_not_found(res):
            return None
        if res.status_code >= 300:
            raise StorageError(f"download failed: HTTP {res.status_code}")
        data = res.content
        content_type = sniff_image_type(data)
        if content_type is None or len(data) > MAX_BYTES:
            return None
        return data, content_type

    def delete_pending(self, session_id: uuid.UUID) -> None:
        """대기 사진 삭제. 없어도 성공. 미설정이면 지울 것도 없으니 그냥 끝. 그 밖의 실패 → StorageError."""
        if not self.configured:
            return
        try:
            res = httpx.delete(
                self._object_url(pending_key(session_id)),
                headers=self._headers(),
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as e:
            raise StorageError(f"delete failed: {type(e).__name__}") from e
        if res.status_code >= 300 and not _is_not_found(res):
            raise StorageError(f"delete failed: HTTP {res.status_code}")

    def attach_to_report(self, session_id: uuid.UUID, report_id: uuid.UUID) -> str | None:
        """대기 사진을 신고 자리로 이동. 성공 → 새 경로, 사진 없음·미설정 → None, 그 밖의 실패 → StorageError."""
        if not self.configured:
            return None
        dest = report_key(report_id)
        try:
            res = httpx.post(
                f"{self._url}/storage/v1/object/move",
                json={"bucketId": BUCKET, "sourceKey": pending_key(session_id), "destinationKey": dest},
                headers=self._headers(),
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as e:
            raise StorageError(f"move failed: {type(e).__name__}") from e
        if _is_not_found(res):
            return None
        if res.status_code >= 300:
            raise StorageError(f"move failed: HTTP {res.status_code}")
        return dest

    def signed_url(self, path: str, expires_in: int = SIGNED_URL_SECONDS) -> str | None:
        """임시 링크(https). 미설정·실패면 None(로그만 — 링크·토큰은 남기지 않음)."""
        if not self.configured or not path:
            return None
        try:
            res = httpx.post(
                f"{self._url}/storage/v1/object/sign/{BUCKET}/{path}",
                json={"expiresIn": expires_in},
                headers=self._headers(),
                timeout=TIMEOUT_SECONDS,
            )
            if res.status_code >= 300:
                logger.warning("사진 임시 링크 발급 실패 path=%s HTTP %s", path, res.status_code)
                return None
            signed = res.json().get("signedURL") or res.json().get("signedUrl")
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            logger.warning("사진 임시 링크 발급 실패 path=%s %s", path, type(e).__name__)
            return None
        if not isinstance(signed, str) or not signed:
            logger.warning("사진 임시 링크 응답 형식이 예상과 다름 path=%s", path)
            return None
        if signed.startswith("http"):
            return signed
        return f"{self._url}/storage/v1{signed if signed.startswith('/') else '/' + signed}"

    def pending_signed_url(self, session_id: uuid.UUID, expires_in: int = SIGNED_URL_SECONDS) -> str | None:
        """대기 사진용 임시 링크 — 차원의 AI 사진 분석(후속)용. 이 작업에선 호출하는 곳이 없음."""
        return self.signed_url(pending_key(session_id), expires_in)


def get_photo_storage() -> PhotoStorage:
    """라우터 의존성 — 테스트에서 app.dependency_overrides로 가짜 저장소로 바꿔 끼운다."""
    settings = get_settings()
    return PhotoStorage(settings.supabase_url, settings.supabase_service_key)
