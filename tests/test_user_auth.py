"""학생 회원가입·로그인 (작업 1-24).

- DB 없이: 토큰 종류 구분(학생 토큰 ↔ 관리자 토큰), 입력 형식 검사
- DB 통합: TEST_DATABASE_URL(마이그레이션 끝난 테스트용 빈 DB)이 있을 때만 — 운영 DB 금지!
"""
import os
import uuid
from collections.abc import Iterator

import pytest
from pydantic import ValidationError

TEST_DB = os.environ.get("TEST_DATABASE_URL")
if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB
    os.environ["JWT_SECRET"] = "integration-test-secret-at-least-32-bytes!!"

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import app
from app.models import Admin, User
from app.schemas.user_auth import SignupIn
from app.services import auth, login_guard

needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL 없음 — DB 통합테스트 생략")
PASSWORD = "test-only-password"


# ── DB 없이 ───────────────────────────────────────────────────────────────────
def test_user_and_admin_tokens_are_not_interchangeable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_secret", lambda: "unit-test-secret-at-least-32-bytes!!!!")
    uid = uuid.uuid4()
    user_token = auth.create_access_token(uid, typ=auth.USER_TOKEN_TYPE)
    admin_token = auth.create_access_token(uid)
    assert auth.decode_access_token(user_token, typ=auth.USER_TOKEN_TYPE) == uid
    assert auth.decode_access_token(admin_token) == uid
    assert auth.decode_access_token(user_token) is None  # 학생 토큰으로 관리자 API 불가
    assert auth.decode_access_token(admin_token, typ=auth.USER_TOKEN_TYPE) is None  # 반대도 불가


def test_signup_input_rules() -> None:
    ok = SignupIn(login_id="  Campus_Kim1 ", password="12345678", name="  김  학생 ")
    assert ok.login_id == "campus_kim1" and ok.name == "김 학생"
    for bad in ({"login_id": "ab"}, {"login_id": "한글아이디"}, {"login_id": "a" * 21},
                {"password": "short"}, {"password": "가" * 25}, {"name": "   "}, {"name": "가" * 21}):
        body = {"login_id": "campus_kim", "password": "12345678", "name": "김학생", **bad}
        with pytest.raises(ValidationError):
            SignupIn.model_validate(body)


# ── DB 통합 ───────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    with TestClient(app) as c:
        yield c


def _new_id() -> str:
    return f"t_{uuid.uuid4().hex[:10]}"  # 다시 돌려도 겹치지 않게 매번 새 아이디


@needs_db
def test_signup_then_login_then_me(client: TestClient) -> None:
    login_guard.reset_all()
    login_id = _new_id()
    res = client.post("/api/v1/auth/signup", json={"login_id": login_id, "password": PASSWORD, "name": "김학생"})
    assert res.status_code == 201 and res.json()["name"] == "김학생" and res.json()["token_type"] == "bearer"
    # 비밀번호는 해시로만 저장
    db = get_sessionmaker()()
    saved = db.scalar(select(User).where(User.login_id == login_id))
    db.close()
    assert saved is not None and saved.password_hash != PASSWORD and saved.password_hash.startswith("$2")

    res = client.post("/api/v1/auth/login", json={"login_id": login_id.upper(), "password": PASSWORD})
    assert res.status_code == 200 and res.json()["name"] == "김학생" and res.json()["role"] == "user"
    headers = {"Authorization": f"Bearer {res.json()['access_token']}"}
    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200 and me.json() == {"login_id": login_id, "name": "김학생"}
    # 학생 토큰으로 관리자 API는 못 씀
    assert client.get("/api/v1/admin/reports", headers=headers).status_code == 401


@needs_db
def test_duplicate_wrong_password_and_missing_token(client: TestClient) -> None:
    login_guard.reset_all()
    login_id = _new_id()
    body = {"login_id": login_id, "password": PASSWORD, "name": "이학생"}
    assert client.post("/api/v1/auth/signup", json=body).status_code == 201
    dup = client.post("/api/v1/auth/signup", json=body)
    assert dup.status_code == 409 and "이미 쓰는 아이디" in dup.json()["detail"]
    wrong = client.post("/api/v1/auth/login", json={"login_id": login_id, "password": "wrong-password"})
    nobody = client.post("/api/v1/auth/login", json={"login_id": _new_id(), "password": PASSWORD})
    assert wrong.status_code == nobody.status_code == 401
    assert wrong.json()["detail"] == nobody.json()["detail"]  # 어느 쪽이 틀렸는지 알리지 않음
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401


@needs_db
def test_login_failures_are_limited(client: TestClient) -> None:
    login_guard.reset_all()
    login_id = _new_id()
    assert client.post("/api/v1/auth/signup", json={"login_id": login_id, "password": PASSWORD, "name": "박학생"}).status_code == 201
    for _ in range(5):
        assert client.post("/api/v1/auth/login", json={"login_id": login_id, "password": "wrong-password"}).status_code == 401
    blocked = client.post("/api/v1/auth/login", json={"login_id": login_id, "password": PASSWORD})
    assert blocked.status_code == 429 and blocked.headers["Retry-After"] == "60"
    login_guard.reset_all()


@needs_db
def test_admin_account_logs_in_as_admin_and_cannot_be_taken_by_student(client: TestClient) -> None:
    """같은 로그인 창에서 관리자 계정이면 관리자 토큰(role=admin), 관리자 아이디로 학생 가입은 안 됨 (1-24, 10/30)."""
    import bcrypt

    login_guard.reset_all()
    admin_id = _new_id()
    db = get_sessionmaker()()
    db.add(Admin(login_id=admin_id, password_hash=bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode(), name="테스트관리자"))
    db.commit()
    db.close()
    # 관리자 로그인은 됨
    assert client.post("/api/v1/admin/auth/login", json={"login_id": admin_id, "password": PASSWORD}).status_code == 200
    # 같은 로그인 창(/auth/login)으로 관리자 계정 → role=admin, 관리자 API에 쓸 수 있는 토큰
    res = client.post("/api/v1/auth/login", json={"login_id": admin_id, "password": PASSWORD})
    assert res.status_code == 200 and res.json()["role"] == "admin" and res.json()["name"] == "테스트관리자"
    admin_headers = {"Authorization": f"Bearer {res.json()['access_token']}"}
    assert client.get("/api/v1/admin/reports", headers=admin_headers).status_code == 200
    assert client.get("/api/v1/auth/me", headers=admin_headers).status_code == 401  # 학생 API엔 못 씀
    # 비밀번호가 틀리면 학생과 같은 401 문구
    wrong = client.post("/api/v1/auth/login", json={"login_id": admin_id, "password": "wrong-password"})
    assert wrong.status_code == 401 and wrong.json()["detail"] == "아이디 또는 비밀번호가 맞지 않아요."
    # 관리자 아이디로 학생 가입 불가 (일반 중복과 같은 문구)
    res = client.post("/api/v1/auth/signup", json={"login_id": admin_id, "password": PASSWORD, "name": "흉내"})
    assert res.status_code == 409 and res.json()["detail"] == "이미 쓰는 아이디예요."
    login_guard.reset_all()
