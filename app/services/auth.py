"""관리자 인증 — 비밀번호 확인(bcrypt) + JWT 발급·검증 (명세서 5-1, 11장 "간단한 JWT 로그인").

- 비밀키: 환경변수 JWT_SECRET (GitHub Secret → deploy.yml로 주입). 비어 있으면 토큰을 만들지도
  검사하지도 않고 503을 돌려줌 → 비밀키 없이 서명된 토큰이 돌아다니는 사고 방지.
- 만료: JWT_EXPIRE_MINUTES (기본 60분). 회원가입·토큰 갱신은 없음 (시드 계정 admin1~5만 사용).
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


def create_access_token(admin_id: uuid.UUID, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    expire = now + timedelta(minutes=get_settings().jwt_expire_minutes)
    payload = {"sub": str(admin_id), "iat": now, "exp": expire}
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def decode_access_token(token: str) -> uuid.UUID | None:
    """유효하면 관리자 id, 위조·만료·형식 오류면 None."""
    try:
        payload = jwt.decode(token, _secret(), algorithms=[ALGORITHM], options={"require": ["exp", "sub"]})
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
