"""학생 회원가입·로그인 (명세서 5-1 "사용자용 — 계정", 작업 1-24).

선택 사항: 로그인하지 않아도 챗봇·신고는 익명으로 그대로 쓴다. 계정은 신고·대화와 연결하지 않고,
화면 머리글의 "환영합니다 OOO님!" 표시에만 쓴다.
- 비밀번호는 bcrypt 해시로만 저장. 로그인은 저장된 해시와 대조해 맞을 때만 토큰을 준다.
- 토큰은 관리자와 같은 JWT_SECRET이지만 `typ: "user"`라 관리자 API에는 쓸 수 없다(app/services/auth.py).
- 로그인 창은 하나: 학생(users)이 아니면 관리자(admins)도 확인해서, 관리자면 관리자 토큰과 `role: "admin"`을
  돌려주고 화면이 관리자 페이지로 넘긴다(10/30 변경). 관리자 아이디로 학생 가입은 막는다(같은 아이디가 둘이 되지 않게).
- 남용 막기: 회원가입은 접속 IP당 5회/분, 로그인 실패는 (IP, 아이디)당 5회/분(관리자와 같은 규칙, S-3).
"""
import logging
import uuid
from typing import Annotated

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.admin import Admin
from app.models.user import User
from app.rate_limit import client_ip, limiter
from app.schemas.user_auth import SignupIn, UserLoginIn, UserMe, UserTokenOut
from app.services.auth import (
    NOT_CONFIGURED,
    USER_TOKEN_TYPE,
    AuthNotConfiguredError,
    authenticate,
    create_access_token,
    decode_access_token,
    verify_password,
)
from app.services.login_guard import RETRY_AFTER_SECONDS, is_blocked, record_failure

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["user-auth"])
DbSession = Annotated[Session, Depends(get_db)]
_bearer = HTTPBearer(auto_error=False)
LOGIN_SCOPE = "user-login"
SIGNUP_LIMIT = "5/minute"

_UNAUTHORIZED = HTTPException(
    status_code=401, detail="로그인이 필요해요.", headers={"WWW-Authenticate": "Bearer"}
)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _token_for(user: User) -> UserTokenOut:
    try:
        token = create_access_token(user.id, typ=USER_TOKEN_TYPE)
    except AuthNotConfiguredError:
        raise NOT_CONFIGURED from None
    return UserTokenOut(access_token=token, name=user.name)


def _admin_token_for(admin: Admin) -> UserTokenOut:
    """관리자 계정으로 로그인한 경우 — 관리자 API용 토큰(typ 없음, `/admin/auth/login`과 같은 것)."""
    try:
        token = create_access_token(admin.id)
    except AuthNotConfiguredError:
        raise NOT_CONFIGURED from None
    return UserTokenOut(access_token=token, name=admin.name, role="admin")


@router.post(
    "/signup",
    response_model=UserTokenOut,
    status_code=201,
    responses={409: {"description": "이미 쓰는 아이디"}, 429: {"description": "접속 IP당 회원가입 한도 초과"}},
)
@limiter.limit(SIGNUP_LIMIT, key_func=client_ip)
def signup(request: Request, body: SignupIn, db: DbSession) -> UserTokenOut:
    """회원가입 → 바로 로그인된 토큰을 준다."""
    taken = db.scalar(select(User.id).where(User.login_id == body.login_id)) is not None
    # 관리자 아이디(admins 테이블)로는 학생 가입 불가 — 같은 아이디로 학생 계정이 생겨 관리자 계정과 헷갈리지 않게.
    # 아이디가 관리자 것인지는 알리지 않고 일반 중복과 같은 문구로 거절 (관리자 아이디 노출 방지)
    taken = taken or db.scalar(select(Admin.id).where(func.lower(Admin.login_id) == body.login_id)) is not None
    if taken:
        raise HTTPException(status_code=409, detail="이미 쓰는 아이디예요.")
    user = User(login_id=body.login_id, password_hash=hash_password(body.password), name=body.name)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:  # 동시에 같은 아이디로 가입한 경우
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 쓰는 아이디예요.") from None
    db.refresh(user)
    logger.info("학생 회원가입 %s", user.login_id)
    return _token_for(user)


@router.post(
    "/login",
    response_model=UserTokenOut,
    responses={401: {"description": "아이디 또는 비밀번호가 맞지 않음"}, 429: {"description": "로그인 실패 한도 초과"}},
)
def login(request: Request, body: UserLoginIn, db: DbSession) -> UserTokenOut:
    """저장된 비밀번호 해시와 대조해 맞으면 토큰을 준다. 학생 계정이 아니면 관리자 계정도 확인한다.
    어느 쪽이 틀렸는지(아이디가 있는지, 관리자인지)는 알리지 않는다."""
    if is_blocked(request, body.login_id, LOGIN_SCOPE):
        raise HTTPException(
            status_code=429,
            detail="로그인 실패가 너무 많아요. 1분 뒤에 다시 시도해 주세요.",
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
    user = db.scalar(select(User).where(User.login_id == body.login_id))
    if verify_password(body.password, user.password_hash if user else None) and user is not None:
        return _token_for(user)
    admin = authenticate(db, body.login_id, body.password)
    if admin is not None:
        logger.info("관리자 로그인(통합 로그인 창) %s", admin.login_id)
        return _admin_token_for(admin)
    record_failure(request, body.login_id, LOGIN_SCOPE)
    raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 맞지 않아요.")


def get_current_user(
    db: DbSession,
    cred: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if cred is None:
        raise _UNAUTHORIZED
    try:
        user_id: uuid.UUID | None = decode_access_token(cred.credentials, typ=USER_TOKEN_TYPE)
    except AuthNotConfiguredError:
        raise NOT_CONFIGURED from None
    user = db.get(User, user_id) if user_id else None
    if user is None:
        raise _UNAUTHORIZED
    return user


@router.get("/me", response_model=UserMe, responses={401: {"description": "토큰 없음·만료·위조"}})
def me(user: Annotated[User, Depends(get_current_user)]) -> UserMe:
    """로그인한 학생 이름 — 새로고침 뒤 머리글에 "환영합니다 OOO님!"을 다시 보여줄 때."""
    return UserMe(login_id=user.login_id, name=user.name)
