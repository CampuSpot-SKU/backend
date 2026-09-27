"""관리자 계정 — 회원가입 없이 시드로 미리 생성 (명세서 11장).

로그인은 login_id + 비밀번호. 비밀번호는 평문 저장 금지, bcrypt 해시만 저장.
"""
import uuid

from sqlalchemy import String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models._common import uuid_pk


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[uuid.UUID] = uuid_pk()
    login_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    dept: Mapped[str | None] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'admin'"))
