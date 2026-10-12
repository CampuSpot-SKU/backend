"""관리자 계정 만들기·비밀번호 교체 (작업 S-3).

입력 검사·계획은 DB 없이, 실제 저장은 TEST_DATABASE_URL이 있을 때만(운영 DB 금지!) 확인한다.
테스트용 비밀번호는 모두 가짜 값 — 실제 관리자 비밀번호는 레포에 넣지 않는다.
"""
import json
import os
import uuid
from typing import Any

import bcrypt
import pytest

from app.models.admin import Admin
from app.services import admin_accounts as aa
from app.services.admin_accounts import AccountInputError, apply_accounts, parse_accounts

FAKE_PUBLIC = "fake-public-initial-pw"
PW = "test-only-password-1"


@pytest.fixture(autouse=True)
def _fast_bcrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    # 테스트에선 공개 초기 해시를 알고 있는 가짜 값으로 바꾸고, bcrypt를 빠르게
    monkeypatch.setattr(aa, "PUBLIC_INITIAL_HASH",
                        bcrypt.hashpw(FAKE_PUBLIC.encode(), bcrypt.gensalt(4)).decode())
    monkeypatch.setattr(aa, "BCRYPT_ROUNDS", 4)


def _raw(*items: dict[str, Any]) -> str:
    return json.dumps(list(items))


# ── 입력 검사 ──────────────────────────────────────────────

def test_parse_ok() -> None:
    specs = parse_accounts(_raw({"login_id": "judge1", "password": PW, "name": "심사위원1"},
                                {"login_id": "admin1", "password": PW + "x"}))
    assert [s.login_id for s in specs] == ["judge1", "admin1"]
    assert specs[0].name == "심사위원1" and specs[1].name is None


@pytest.mark.parametrize(
    ("raw", "msg"),
    [
        ("not json", "JSON이 아니에요"),
        ("[]", "1개 이상"),
        ('{"login_id": "a"}', "1개 이상"),
        (_raw({"login_id": "a b", "password": PW}), "login_id"),
        (_raw({"login_id": "admin1", "password": "short"}), "12자 이상"),
        (_raw({"login_id": "admin1", "password": "가" * 25}), "72바이트"),
        (_raw({"login_id": "admin1", "password": PW}, {"login_id": "admin1", "password": PW}),
         "중복"),
        (_raw({"login_id": "admin1", "password": FAKE_PUBLIC}), "공개된 초기 비밀번호"),
        (_raw({"login_id": "admin1", "password": PW, "name": ""}), "name"),
    ],
)
def test_parse_rejects(raw: str, msg: str) -> None:
    with pytest.raises(AccountInputError) as e:
        parse_accounts(raw)
    assert msg in str(e.value)
    assert PW not in str(e.value)  # 오류 메시지에 비밀번호를 넣지 않음


# ── 계획(가짜 DB) ──────────────────────────────────────────

class _FakeDb:
    def __init__(self, admins: list[Admin]) -> None:
        self.admins = admins
        self.committed = False

    def scalars(self, _stmt: Any) -> list[Admin]:
        return list(self.admins)

    def add(self, a: Admin) -> None:
        self.admins.append(a)

    def commit(self) -> None:
        self.committed = True


def _admin(login_id: str, password_hash: str) -> Admin:
    return Admin(login_id=login_id, password_hash=password_hash, name=login_id)


def test_dry_run_changes_nothing() -> None:
    old = _admin("admin1", aa.PUBLIC_INITIAL_HASH)
    db = _FakeDb([old, _admin("admin2", aa.PUBLIC_INITIAL_HASH)])
    specs = parse_accounts(_raw({"login_id": "admin1", "password": PW},
                                {"login_id": "judge1", "password": PW, "name": "심사위원1"}))
    result = apply_accounts(db, specs, dry_run=True)  # type: ignore[arg-type]
    assert [(c.login_id, c.action) for c in result.changes] == [("admin1", "update"),
                                                                ("judge1", "create")]
    assert result.still_public == ["admin2"]
    assert old.password_hash == aa.PUBLIC_INITIAL_HASH and len(db.admins) == 2
    assert not db.committed


def test_apply_updates_and_creates() -> None:
    old = _admin("admin1", aa.PUBLIC_INITIAL_HASH)
    db = _FakeDb([old])
    specs = parse_accounts(_raw({"login_id": "admin1", "password": PW},
                                {"login_id": "judge1", "password": PW + "2", "name": "심사위원1",
                                 "dept": "심사"}))
    result = apply_accounts(db, specs, dry_run=False)  # type: ignore[arg-type]
    assert result.still_public == [] and db.committed
    assert bcrypt.checkpw(PW.encode(), old.password_hash.encode())
    new = next(a for a in db.admins if a.login_id == "judge1")
    assert bcrypt.checkpw((PW + "2").encode(), new.password_hash.encode())
    assert new.name == "심사위원1" and new.dept == "심사"


def test_new_account_needs_name() -> None:
    specs = parse_accounts(_raw({"login_id": "judge9", "password": PW}))
    with pytest.raises(AccountInputError, match="name"):
        apply_accounts(_FakeDb([]), specs, dry_run=True)  # type: ignore[arg-type]


# ── 실제 DB (TEST_DATABASE_URL 있을 때만) ───────────────────

TEST_DB = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL 없음 — DB 통합테스트 생략")
def test_apply_on_real_db_then_login() -> None:
    assert TEST_DB
    os.environ["DATABASE_URL"] = TEST_DB
    from app.config import get_settings
    from app.db.session import get_engine, get_sessionmaker
    from app.services.auth import authenticate

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    login_id = f"t_{uuid.uuid4().hex[:8]}"  # 다시 돌려도 겹치지 않게
    db = get_sessionmaker()()
    try:
        first = apply_accounts(db, parse_accounts(_raw(
            {"login_id": login_id, "password": PW, "name": "테스트"})), dry_run=False)
        assert [c.action for c in first.changes] == ["create"]
        assert authenticate(db, login_id, PW) is not None

        second = apply_accounts(db, parse_accounts(_raw(
            {"login_id": login_id, "password": PW + "new"})), dry_run=False)
        assert [c.action for c in second.changes] == ["update"]
        db.expire_all()
        assert authenticate(db, login_id, PW) is None
        assert authenticate(db, login_id, PW + "new") is not None
    finally:
        db.close()
