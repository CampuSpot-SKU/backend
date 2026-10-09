"""사진 업로드 (작업 1-10) — DB·실제 저장소 없이 도는 테스트 (CI).

검사 함수, Storage REST 호출 모양(가짜 httpx), 업로드/삭제 라우트(가짜 DB·가짜 저장소)를 확인한다.
DB가 필요한 접수·관리자 흐름은 test_chat_api.py·test_admin_api.py.
"""
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app
from app.services import photo_storage as ps
from app.services.photo_storage import (
    MAX_BYTES,
    PhotoStorage,
    PhotoTooLargeError,
    StorageError,
    UnsupportedPhotoError,
    get_photo_storage,
    sniff_image_type,
    validate_image,
)

JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"
PNG_HEAD = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
URL = "https://example.supabase.co"
KEY = "test-service-key"

# 2장 계약 문구 — 화면이 그대로 보여주므로 글자까지 같아야 함
NO_SESSION = "대화 세션을 찾을 수 없어요."
TOO_LARGE = "사진은 5MB 이하만 올릴 수 있어요."
UNSUPPORTED = "jpg 또는 png 사진만 올릴 수 있어요."
STORAGE_FAILED = "사진을 저장하지 못했어요. 잠시 후 다시 시도해 주세요."


# ---------- 1-1 ~ 1-3 검사 함수 ----------

def test_sniff_jpeg_and_png() -> None:
    assert sniff_image_type(JPEG_HEAD + b"rest") == "image/jpeg"
    assert sniff_image_type(PNG_HEAD + b"rest") == "image/png"


@pytest.mark.parametrize("data", [
    b"GIF89a\x01\x00", b"%PDF-1.7\n", b"RIFF\x00\x00\x00\x00WEBPVP8 ", b"hello", b"", b"\xff\xd8",
])
def test_sniff_rejects_other_formats(data: bytes) -> None:
    assert sniff_image_type(data) is None
    with pytest.raises(UnsupportedPhotoError):
        validate_image(data)


def test_size_limit_exactly_5mb_passes_one_more_byte_fails() -> None:
    ok = JPEG_HEAD + b"\x00" * (MAX_BYTES - len(JPEG_HEAD))
    assert len(ok) == MAX_BYTES
    assert validate_image(ok) == "image/jpeg"
    with pytest.raises(PhotoTooLargeError):
        validate_image(ok + b"\x00")


# ---------- 1-4 ~ 1-8 Storage REST 호출 (가짜 httpx) ----------

class FakeHttp:
    """httpx.post/delete 대신 호출 내용을 기록하고 정해 둔 응답을 돌려줌."""

    def __init__(self, status: int = 200, body: Any = None, text: str | None = None) -> None:
        self.status, self.body, self.text = status, body, text
        self.calls: list[dict[str, Any]] = []

    def _res(self, method: str, url: str) -> httpx.Response:
        req = httpx.Request(method, url)
        if self.text is not None:
            return httpx.Response(self.status, text=self.text, request=req)
        return httpx.Response(self.status, json=self.body if self.body is not None else {}, request=req)

    def post(self, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"method": "POST", "url": url, **kw})
        return self._res("POST", url)

    def get(self, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"method": "GET", "url": url, **kw})
        return self._res("GET", url)

    def delete(self, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"method": "DELETE", "url": url, **kw})
        return self._res("DELETE", url)


@pytest.fixture()
def http(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeHttp]:
    fake = FakeHttp()
    monkeypatch.setattr(ps.httpx, "post", fake.post)
    monkeypatch.setattr(ps.httpx, "get", fake.get)
    monkeypatch.setattr(ps.httpx, "delete", fake.delete)
    yield fake


def storage() -> PhotoStorage:
    return PhotoStorage(URL + "/", KEY)  # 끝의 / 유무와 상관없이 동작해야 함


def test_upload_pending_request_shape(http: FakeHttp) -> None:
    sid = uuid.uuid4()
    data = JPEG_HEAD + b"abc"
    storage().upload_pending(sid, data, "image/jpeg")
    (call,) = http.calls
    assert call["url"] == f"{URL}/storage/v1/object/report-photos/pending/{sid}/photo"
    assert call["content"] == data
    h = call["headers"]
    assert h["x-upsert"] == "true" and h["Content-Type"] == "image/jpeg"
    assert h["Authorization"] == f"Bearer {KEY}" and h["apikey"] == KEY
    assert call["timeout"] == 15.0


def test_upload_pending_failure_raises_without_secret(http: FakeHttp) -> None:
    http.status = 500
    with pytest.raises(StorageError) as e:
        storage().upload_pending(uuid.uuid4(), JPEG_HEAD, "image/jpeg")
    assert KEY not in str(e.value)


def test_attach_moves_pending_to_report(http: FakeHttp) -> None:
    sid, rid = uuid.uuid4(), uuid.uuid4()
    http.body = {"message": "Successfully moved"}
    assert storage().attach_to_report(sid, rid) == f"reports/{rid}/photo"
    (call,) = http.calls
    assert call["url"] == f"{URL}/storage/v1/object/move"
    assert call["json"] == {
        "bucketId": "report-photos",
        "sourceKey": f"pending/{sid}/photo",
        "destinationKey": f"reports/{rid}/photo",
    }


@pytest.mark.parametrize(("status", "text"), [(404, '{"error":"x"}'), (400, '{"message":"Object not found"}')])
def test_attach_without_photo_is_none(http: FakeHttp, status: int, text: str) -> None:
    http.status, http.text = status, text
    assert storage().attach_to_report(uuid.uuid4(), uuid.uuid4()) is None


def test_attach_server_error_raises(http: FakeHttp) -> None:
    http.status = 500
    with pytest.raises(StorageError):
        storage().attach_to_report(uuid.uuid4(), uuid.uuid4())


def test_delete_pending_missing_is_ok(http: FakeHttp) -> None:
    sid = uuid.uuid4()
    http.status = 404
    storage().delete_pending(sid)
    assert http.calls[0]["method"] == "DELETE"
    assert http.calls[0]["url"] == f"{URL}/storage/v1/object/report-photos/pending/{sid}/photo"
    http.status = 500
    with pytest.raises(StorageError):
        storage().delete_pending(sid)


def test_signed_url_builds_full_link(http: FakeHttp) -> None:
    http.body = {"signedURL": "/object/sign/report-photos/x?token=abc"}
    assert storage().signed_url("reports/x/photo") == (
        f"{URL}/storage/v1/object/sign/report-photos/x?token=abc"
    )
    call = http.calls[0]
    assert call["url"] == f"{URL}/storage/v1/object/sign/report-photos/reports/x/photo"
    assert call["json"] == {"expiresIn": 600}


def test_signed_url_failure_is_none(http: FakeHttp) -> None:
    http.status = 400
    assert storage().signed_url("reports/x/photo") is None
    http.status, http.body = 200, {"unexpected": True}
    assert storage().signed_url("reports/x/photo") is None


def test_pending_signed_url_uses_pending_key(http: FakeHttp) -> None:
    sid = uuid.uuid4()
    http.body = {"signedURL": "/object/sign/report-photos/p?token=t"}
    assert storage().pending_signed_url(sid) is not None
    assert http.calls[0]["url"].endswith(f"/object/sign/report-photos/pending/{sid}/photo")


def test_not_configured(http: FakeHttp) -> None:
    s = PhotoStorage("", "")
    assert s.attach_to_report(uuid.uuid4(), uuid.uuid4()) is None
    assert s.signed_url("reports/x/photo") is None
    s.delete_pending(uuid.uuid4())  # 지울 것도 없음 → 조용히 끝
    with pytest.raises(StorageError):
        s.upload_pending(uuid.uuid4(), JPEG_HEAD, "image/jpeg")
    assert http.calls == []  # 미설정이면 외부 호출 없음


# ---------- 1-9 ~ 1-10 라우트 (가짜 DB·가짜 저장소) ----------

class _Db:
    """세션 존재 여부만 정해 두는 가짜 DB 세션."""

    def __init__(self, exists: bool) -> None:
        self.exists = exists

    def get(self, *_a: Any) -> object | None:
        return object() if self.exists else None


class FakeStorage(PhotoStorage):
    def __init__(self, fail: bool = False) -> None:
        super().__init__("", "")
        self.fail = fail
        self.pending: dict[uuid.UUID, tuple[bytes, str]] = {}

    def upload_pending(self, session_id: uuid.UUID, data: bytes, content_type: str) -> None:
        if self.fail:
            raise StorageError("down")
        self.pending[session_id] = (data, content_type)

    def delete_pending(self, session_id: uuid.UUID) -> None:
        if self.fail:
            raise StorageError("down")
        self.pending.pop(session_id, None)


@pytest.fixture()
def route() -> Iterator[tuple[TestClient, _Db, FakeStorage]]:
    db, store = _Db(exists=True), FakeStorage()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_photo_storage] = lambda: store
    yield TestClient(app), db, store
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_photo_storage, None)


def _post(client: TestClient, sid: uuid.UUID, data: bytes, name: str = "a.jpg") -> httpx.Response:
    # 파일 이름·Content-Type은 일부러 엉뚱하게 — 서버는 첫 바이트로만 판단해야 함
    return client.post(
        f"/api/v1/chat/sessions/{sid}/photo", files={"file": (name, data, "application/octet-stream")}
    )


def test_upload_jpeg_201(route: tuple[TestClient, _Db, FakeStorage]) -> None:
    client, _, store = route
    sid = uuid.uuid4()
    data = JPEG_HEAD + b"\x00" * (2 * 1024 * 1024)
    res = _post(client, sid, data, name="../../evil.png")
    assert res.status_code == 201
    assert res.json() == {"attached": True, "content_type": "image/jpeg", "size_bytes": len(data)}
    assert store.pending[sid] == (data, "image/jpeg")


def test_upload_png_replaces_previous(route: tuple[TestClient, _Db, FakeStorage]) -> None:
    client, _, store = route
    sid = uuid.uuid4()
    assert _post(client, sid, JPEG_HEAD).status_code == 201
    assert _post(client, sid, PNG_HEAD).json()["content_type"] == "image/png"
    assert store.pending[sid][1] == "image/png" and len(store.pending) == 1


def test_upload_errors_match_contract(route: tuple[TestClient, _Db, FakeStorage]) -> None:
    client, db, store = route
    sid = uuid.uuid4()
    res = _post(client, sid, b"GIF89a....", name="cat.jpg")
    assert (res.status_code, res.json()) == (415, {"detail": UNSUPPORTED})
    res = _post(client, sid, b"hello", name="note.jpg")
    assert (res.status_code, res.json()) == (415, {"detail": UNSUPPORTED})
    res = _post(client, sid, JPEG_HEAD + b"\x00" * (MAX_BYTES + 1 - len(JPEG_HEAD)))
    assert (res.status_code, res.json()) == (413, {"detail": TOO_LARGE})
    assert client.post(f"/api/v1/chat/sessions/{sid}/photo").status_code == 422
    store.fail = True
    res = _post(client, sid, JPEG_HEAD)
    assert (res.status_code, res.json()) == (503, {"detail": STORAGE_FAILED})
    db.exists = False
    res = _post(client, sid, JPEG_HEAD)
    assert (res.status_code, res.json()) == (404, {"detail": NO_SESSION})
    assert store.pending == {}


def test_delete_204_even_without_photo(route: tuple[TestClient, _Db, FakeStorage]) -> None:
    client, db, store = route
    sid = uuid.uuid4()
    _post(client, sid, JPEG_HEAD)
    assert client.delete(f"/api/v1/chat/sessions/{sid}/photo").status_code == 204
    assert sid not in store.pending
    assert client.delete(f"/api/v1/chat/sessions/{sid}/photo").status_code == 204
    db.exists = False
    res = client.delete(f"/api/v1/chat/sessions/{sid}/photo")
    assert (res.status_code, res.json()) == (404, {"detail": NO_SESSION})


def test_bad_session_id_is_422(route: tuple[TestClient, _Db, FakeStorage]) -> None:
    client, _, _ = route
    res = client.post("/api/v1/chat/sessions/not-a-uuid/photo", files={"file": ("a", JPEG_HEAD)})
    assert res.status_code == 422


# ---------- AI 사진 분석용 내려받기 (1-10) ----------

def test_download_pending_returns_bytes_and_sniffed_type(http: FakeHttp) -> None:
    sid = uuid.uuid4()
    http.text = ""
    data = PNG_HEAD + b"abc"
    orig = http._res
    http._res = lambda m, u: httpx.Response(200, content=data, request=httpx.Request(m, u))  # type: ignore[method-assign]
    assert storage().download_pending(sid) == (data, "image/png")
    assert http.calls[0]["url"] == f"{URL}/storage/v1/object/report-photos/pending/{sid}/photo"
    http._res = orig  # type: ignore[method-assign]


def test_download_pending_missing_is_none(http: FakeHttp) -> None:
    http.status, http.text = 404, '{"error":"x"}'
    assert storage().download_pending(uuid.uuid4()) is None


def test_download_pending_server_error_raises(http: FakeHttp) -> None:
    http.status = 500
    with pytest.raises(StorageError):
        storage().download_pending(uuid.uuid4())


def test_download_pending_unconfigured_is_none() -> None:
    assert PhotoStorage("", "").download_pending(uuid.uuid4()) is None


def test_download_pending_rejects_non_image(http: FakeHttp) -> None:
    http.text = "hello"
    assert storage().download_pending(uuid.uuid4()) is None
