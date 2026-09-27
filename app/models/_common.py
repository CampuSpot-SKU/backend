"""모델 공통 컬럼/타입 헬퍼."""
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


def uuid_pk() -> Mapped[uuid.UUID]:
    """PK는 전부 UUID, DB에서 gen_random_uuid()로 생성 (명세서 5장)."""
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def pg_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    """Python Enum의 value(한글 등)를 그대로 DB ENUM 값으로 저장."""
    return Enum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])
