"""학생 회원가입·로그인 요청/응답 (명세서 5-1, 작업 1-24)."""
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

LOGIN_ID_RE = re.compile(r"^[a-z0-9_]{4,20}$")


def _normalize_login_id(v: object) -> object:
    return v.strip().lower() if isinstance(v, str) else v


class SignupIn(BaseModel):
    login_id: str = Field(examples=["campus_kim"], description="4~20자 영문 소문자·숫자·_ (대문자는 소문자로 바꿔 저장)")
    password: str = Field(min_length=8, max_length=72)
    name: str = Field(examples=["김학생"], description="1~20자 — 화면의 '환영합니다 OOO님!'에 씀 (닉네임도 됨)")

    @field_validator("login_id", mode="before")
    @classmethod
    def _login_id(cls, v: object) -> object:
        v = _normalize_login_id(v)
        if not isinstance(v, str) or not LOGIN_ID_RE.match(v):
            raise ValueError("아이디는 4~20자 영문 소문자·숫자·_만 쓸 수 있어요.")
        return v

    @field_validator("password")
    @classmethod
    def _password(cls, v: str) -> str:
        if len(v.encode()) > 72:  # bcrypt가 72바이트까지만 읽음 — 한글은 글자당 3바이트
            raise ValueError("비밀번호가 너무 길어요.")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not 1 <= len(v) <= 20:
            raise ValueError("이름은 1~20자로 적어 주세요.")
        return v


class UserLoginIn(BaseModel):
    login_id: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=100)

    @field_validator("login_id", mode="before")
    @classmethod
    def _login_id(cls, v: object) -> object:
        return _normalize_login_id(v)


class UserTokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    name: str


class UserMe(BaseModel):
    login_id: str
    name: str
