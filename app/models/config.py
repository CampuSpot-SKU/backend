"""학교별 설정 테이블 — 코드 상수 대신 DB에서 관리 (명세서 5장, 11장 '학교별 확장성')."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models._common import created_at, pg_enum, uuid_pk
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


class AutomationRule(Base):
    """조건-액션 규칙 (1-21, 명세 3-1) — 카테고리(+건물)가 맞으면 매트릭스 결과 대신 이 우선순위로 접수."""

    __tablename__ = "automation_rules"
    # 같은 (카테고리, 건물) 조합은 하나만 — 건물이 비어 있는(모든 건물) 규칙끼리도 겹치지 않게 NULLS NOT DISTINCT
    __table_args__ = (
        Index("uq_automation_rules_target", "category_id", "building_id", unique=True,
              postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("categories.id"), nullable=False)
    # 비우면 모든 건물. 건물을 설정에서 지우면 그 건물 규칙도 같이 지워짐
    building_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("buildings.id", ondelete="CASCADE"))
    resulting_priority: Mapped[Priority] = mapped_column(pg_enum(Priority, "priority"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = created_at()
