"""학생 계정 — 선택 사항인 회원가입·로그인 (명세서 5장 users, 작업 1-24).

로그인하지 않아도 챗봇·신고는 익명으로 그대로 쓴다. 계정과 연결되는 것은 로그인한 학생이 claim한 대화 세션뿐이다
(chat_sessions.user_id → users.id, 이전 대화 조회용, 작업 1-27). 신고(reports)에는 계정을 연결하지 않는다.
비밀번호는 평문 저장 금지 — bcrypt 해시만 저장. 학번·전화번호·이메일은 받지 않는다(명세서 11장).
"""
import uuid
from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models._common import created_at, uuid_pk


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    login_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = created_at()
