"""학교별 설정 테이블 — 코드 상수 대신 DB에서 관리 (명세서 5장, 11장 '학교별 확장성')."""
import uuid

from sqlalchemy import Boolean, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models._common import pg_enum, uuid_pk
from app.models.enums import Level, Priority


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class Building(Base):
    __tablename__ = "buildings"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # 챗봇 대화에서 다르게 부르는 이름 (예: "3동", "공학관") — 위치 매칭용
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )


class PriorityMatrixRule(Base):
    __tablename__ = "priority_matrix_rules"
    __table_args__ = (UniqueConstraint("impact", "urgency"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    impact: Mapped[Level] = mapped_column(pg_enum(Level, "level"), nullable=False)
    urgency: Mapped[Level] = mapped_column(pg_enum(Level, "level"), nullable=False)
    resulting_priority: Mapped[Priority] = mapped_column(
        pg_enum(Priority, "priority"), nullable=False
    )


class SlaConfig(Base):
    __tablename__ = "sla_config"

    id: Mapped[uuid.UUID] = uuid_pk()
    priority: Mapped[Priority] = mapped_column(
        pg_enum(Priority, "priority"), unique=True, nullable=False
    )
    sla_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    escalation_50pct_action: Mapped[str | None] = mapped_column(Text)
    escalation_100pct_action: Mapped[str | None] = mapped_column(Text)
    escalation_150pct_action: Mapped[str | None] = mapped_column(Text)


class DetectionConfig(Base):
    __tablename__ = "detection_config"

    id: Mapped[uuid.UUID] = uuid_pk()
    threshold_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    threshold_hours: Mapped[int] = mapped_column(Integer, nullable=False, server_default="72")
