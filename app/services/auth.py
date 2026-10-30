"""관리자 인증 — 비밀번호 확인(bcrypt) + JWT 발급·검증 (명세서 5-1, 11장 "간단한 JWT 로그인").

- 비밀키: 환경변수 JWT_SECRET (GitHub Secret → deploy.yml로 주입). 비어 있으면 토큰을 만들지도
  검사하지도 않고 503을 돌려줌 → 비밀키 없이 서명된 토큰이 돌아다니는 사고 방지.
- 만료: JWT_EXPIRE_MINUTES (기본 60분). 관리자는 회원가입·토큰 갱신 없음 (Secret으로 만든 계정만 사용).
- 학생 계정 토큰(1-24, `app/routers/user_auth.py`)도 같은 비밀키로 만들지만 `typ: "user"`가 들어 있어
  관리자 API에서는 거절되고, 반대로 관리자 토큰(`typ` 없음)은 학생 API에서 거절된다.
"""
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_db
from app.models.admin import Admin

ALGORITHM = "HS256"
# 없는 아이디로 로그인해도 bcrypt 검사 시간은 똑같이 걸리게 하는 더미 해시 (아이디 존재 여부 노출 방지).
# 아무 의미 없는 문자열의 해시라 비밀 아님.
_DUMMY_HASH = b"$2b$12$RQB.co.ApTWWWuRHRk2cw.2IpbkpOJg9NN567BClxmdpuJWzCfYJG"

_bearer = HTTPBearer(auto_error=False)


class AuthNotConfiguredError(Exception):
    """JWT_SECRET이 설정되지 않음."""


def _secret() -> str:
    secret = get_settings().jwt_secret
    if not secret:
        raise AuthNotConfiguredError
    return secret


def verify_password(plain: str, password_hash: str | None) -> bool:
    try:
        ok = bcrypt.checkpw(plain.encode(), (password_hash or "").encode() or _DUMMY_HASH)
    except ValueError:  # 72바이트 초과 비밀번호, 깨진 해시 등
        return False
    return ok and password_hash is not None


def authenticate(db: Session, login_id: str, password: str) -> Admin | None:
    admin = db.scalar(select(Admin).where(Admin.login_id == login_id))
    if not verify_password(password, admin.password_hash if admin else None):
        return None
    return admin


USER_TOKEN_TYPE = "user"  # 학생 계정 토큰 표시 (관리자 토큰엔 typ 없음)


def create_access_token(subject_id: uuid.UUID, now: datetime | None = None, typ: str | None = None) -> str:
    now = now or datetime.now(UTC)
    expire = now + timedelta(minutes=get_settings().jwt_expire_minutes)
    payload: dict[str, object] = {"sub": str(subject_id), "iat": now, "exp": expire}
    if typ:
        payload["typ"] = typ
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def decode_access_token(token: str, typ: str | None = None) -> uuid.UUID | None:
    """유효하면 id, 위조·만료·형식 오류·토큰 종류가 다르면 None. typ=None이면 관리자 토큰만 받음."""
    try:
        payload = jwt.decode(token, _secret(), algorithms=[ALGORITHM], options={"require": ["exp", "sub"]})
        if payload.get("typ") != typ:  # 학생 토큰으로 관리자 API를 부르거나 그 반대인 경우
            return None
        return uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, ValueError):
        return None


_UNAUTHORIZED = HTTPException(
    status_code=401,
    detail="로그인이 필요해요.",
    headers={"WWW-Authenticate": "Bearer"},
)
NOT_CONFIGURED = HTTPException(status_code=503, detail="관리자 로그인 설정이 아직 안 됐어요 (JWT_SECRET).")


def get_current_admin(
    db: Annotated[Session, Depends(get_db)],
    cred: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Admin:
    """관리자 전용 엔드포인트용 의존성 — app/deps.py의 CurrentAdmin으로 사용."""
    if cred is None:
        raise _UNAUTHORIZED
    try:
        admin_id = decode_access_token(cred.credentials)
    except AuthNotConfiguredError:
        raise NOT_CONFIGURED from None
    admin = db.get(Admin, admin_id) if admin_id else None
    if admin is None:
        raise _UNAUTHORIZED
    return admin
