"""관리자 계정 만들기·비밀번호 교체 (작업 S-3).

공개 레포의 마이그레이션 0002에는 admin1~5 공통 초기 비밀번호의 bcrypt 해시가 들어 있어서,
누구나 그 해시로 비밀번호를 대입해 볼 수 있다. 그래서 실제 비밀번호는 레포 밖(GitHub Secret
`ADMIN_ACCOUNTS`)에 두고, Actions의 "Manage admins" 워크플로가 이 모듈로 DB에 반영한다.

- 입력: JSON 목록 `[{"login_id": "admin1", "password": "...", "name": "관리자1"}, ...]`
  (`name`은 새로 만들 때만 필수, `dept`·`role`은 선택)
- 목록에 있는 계정: 있으면 비밀번호(와 준 경우 이름·부서·역할)를 바꾸고, 없으면 새로 만든다
- 목록에 없는 계정은 건드리지 않는다(삭제 없음). 대신 아직 공개된 초기 비밀번호를 쓰는 계정을 알려준다
- 비밀번호 값은 어떤 출력에도 남기지 않는다
"""
import json
import re
from dataclasses import dataclass
from typing import Any

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.admin import Admin

# 마이그레이션 0002의 공개된 초기 비밀번호 해시 — 이 해시를 아직 쓰는 계정을 찾고,
# 같은 비밀번호를 다시 쓰지 못하게 막는 데만 사용한다.
PUBLIC_INITIAL_HASH = "$2b$12$BQTWjNjES/uPe.bbs6XiaON1SPNSV09yeE6q2k7hnsceZ0FWIFDX2"

BCRYPT_ROUNDS = 12
MIN_PASSWORD_LEN = 12
MAX_PASSWORD_BYTES = 72  # bcrypt가 72바이트 뒤는 무시함
LOGIN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,50}$")


class AccountInputError(ValueError):
    """ADMIN_ACCOUNTS 형식·값 오류 (메시지에 비밀번호를 넣지 않음)."""


@dataclass(frozen=True)
class AccountSpec:
    login_id: str
    password: str
    name: str | None = None
    dept: str | None = None
    role: str | None = None


@dataclass(frozen=True)
class Change:
    login_id: str
    action: str  # "create" | "update"


@dataclass(frozen=True)
class Result:
    changes: list[Change]
    still_public: list[str]  # 반영 뒤에도 공개 초기 비밀번호를 쓰는 계정


def _opt_str(item: dict[str, Any], key: str, max_len: int, where: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise AccountInputError(f"{where}: {key}는 1~{max_len}자 문자열이어야 해요.")
    return value.strip()


def parse_accounts(raw: str) -> list[AccountSpec]:
    """ADMIN_ACCOUNTS JSON을 검사해 계정 목록으로 바꾼다. 하나라도 틀리면 전부 거절."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise AccountInputError("ADMIN_ACCOUNTS가 올바른 JSON이 아니에요.") from None
    if not isinstance(data, list) or not data:
        raise AccountInputError("ADMIN_ACCOUNTS는 계정이 1개 이상 든 JSON 목록이어야 해요.")

    specs: list[AccountSpec] = []
    seen: set[str] = set()
    for n, item in enumerate(data, start=1):
        where = f"{n}번째 계정"
        if not isinstance(item, dict):
            raise AccountInputError(f"{where}: 객체({{...}})여야 해요.")
        login_id = item.get("login_id")
        if not isinstance(login_id, str) or not LOGIN_ID_RE.match(login_id):
            raise AccountInputError(f"{where}: login_id는 영문·숫자·_·- 3~50자여야 해요.")
        where = f"{where}({login_id})"
        if login_id in seen:
            raise AccountInputError(f"{where}: login_id가 중복돼요.")
        seen.add(login_id)
        password = item.get("password")
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
            raise AccountInputError(f"{where}: 비밀번호는 {MIN_PASSWORD_LEN}자 이상이어야 해요.")
        if len(password.encode()) > MAX_PASSWORD_BYTES:
            raise AccountInputError(f"{where}: 비밀번호가 {MAX_PASSWORD_BYTES}바이트를 넘어요.")
        if bcrypt.checkpw(password.encode(), PUBLIC_INITIAL_HASH.encode()):
            raise AccountInputError(f"{where}: 공개된 초기 비밀번호는 다시 쓸 수 없어요.")
        specs.append(
            AccountSpec(
                login_id=login_id,
                password=password,
                name=_opt_str(item, "name", 50, where),
                dept=_opt_str(item, "dept", 100, where),
                role=_opt_str(item, "role", 30, where),
            )
        )
    return specs


def apply_accounts(db: Session, specs: list[AccountSpec], *, dry_run: bool) -> Result:
    """계정을 만들거나 고친다. dry_run이면 바뀔 내용만 계산하고 DB는 그대로 둔다(commit 안 함)."""
    existing = {a.login_id: a for a in db.scalars(select(Admin))}
    changes: list[Change] = []
    for spec in specs:
        admin = existing.get(spec.login_id)
        if admin is None and spec.name is None:
            raise AccountInputError(f"{spec.login_id}: 새로 만드는 계정은 name이 필요해요.")
        changes.append(Change(spec.login_id, "update" if admin else "create"))
        if dry_run:
            continue
        password_hash = bcrypt.hashpw(
            spec.password.encode(), bcrypt.gensalt(BCRYPT_ROUNDS)
        ).decode()
        if admin is None:
            assert spec.name is not None
            admin = Admin(login_id=spec.login_id, password_hash=password_hash, name=spec.name)
            if spec.role:
                admin.role = spec.role
            admin.dept = spec.dept
            db.add(admin)
            existing[spec.login_id] = admin
        else:
            admin.password_hash = password_hash
            if spec.name:
                admin.name = spec.name
            if spec.dept:
                admin.dept = spec.dept
            if spec.role:
                admin.role = spec.role

    targets = {s.login_id for s in specs}
    still_public = sorted(
        login_id
        for login_id, a in existing.items()
        if login_id not in targets and a.password_hash == PUBLIC_INITIAL_HASH
    )
    if not dry_run:
        db.commit()
    return Result(changes=changes, still_public=still_public)
