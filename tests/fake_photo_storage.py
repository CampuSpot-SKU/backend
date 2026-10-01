"""DB 통합 테스트용 가짜 사진 저장소 (작업 1-10) — 실제 Supabase Storage를 절대 건드리지 않게 갈아끼운다.

사용: app.dependency_overrides[get_photo_storage] = lambda: FAKE_STORAGE
"""
import uuid

from app.services.photo_storage import PhotoStorage, StorageError, report_key

SIGNED_PREFIX = "https://signed.test/"


class FakePhotoStorage(PhotoStorage):
    """메모리에 대기 사진·붙은 사진을 기록. fail_attach=True면 접수 때 붙이기가 StorageError."""

    def __init__(self) -> None:
        super().__init__("", "")
        self.pending: dict[uuid.UUID, tuple[bytes, str]] = {}
        self.attached: dict[uuid.UUID, str] = {}
        self.fail_attach = False

    def upload_pending(self, session_id: uuid.UUID, data: bytes, content_type: str) -> None:
        self.pending[session_id] = (data, content_type)

    def delete_pending(self, session_id: uuid.UUID) -> None:
        self.pending.pop(session_id, None)

    def attach_to_report(self, session_id: uuid.UUID, report_id: uuid.UUID) -> str | None:
        if self.fail_attach:
            raise StorageError("storage down")
        if self.pending.pop(session_id, None) is None:
            return None
        path = report_key(report_id)
        self.attached[report_id] = path
        return path

    def signed_url(self, path: str, expires_in: int = 600) -> str | None:
        return f"{SIGNED_PREFIX}{path}?expires={expires_in}" if path else None


FAKE_STORAGE = FakePhotoStorage()
