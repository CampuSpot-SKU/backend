"""시설물 신고(인시던트)와 상태 이력."""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models._common import created_at, pg_enum, uuid_pk
from app.models.enums import Priority, ReportStatus


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        Index("ix_reports_status", "status"),
        Index("ix_reports_created_at", "created_at"),
        # 탐지(같은 건물+카테고리, 최근 N시간) 조회용
        Index("ix_reports_building_category_created", "building_id", "category_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # 사용자에게 안내하는 짧은 순차 접수번호 (1, 2, 3...) — UUID는 길어서 안내용으로 부적합
    display_no: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), unique=True, nullable=False
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="SET NULL")
    )
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("categories.id"), nullable=False)
    priority: Mapped[Priority] = mapped_column(pg_enum(Priority, "priority"), nullable=False)
    status: Mapped[ReportStatus] = mapped_column(
        pg_enum(ReportStatus, "report_status"),
        nullable=False,
        server_default=ReportStatus.RECEIVED.value,
    )
    # 위치: 건물(FK) + 층 + 세부위치. 건물 매칭 실패 시 location_raw에 자유텍스트로 저장
    building_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("buildings.id"))
    floor: Mapped[str | None] = mapped_column(String(20))  # "2", "B1" 등
    detail: Mapped[str | None] = mapped_column(String(100))  # "화장실" 등
    location_raw: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    photo_url: Mapped[str | None] = mapped_column(Text)
    assigned_dept: Mapped[str | None] = mapped_column(String(100))
    sla_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ReportStatusHistory(Base):
    __tablename__ = "report_status_history"

    id: Mapped[uuid.UUID] = uuid_pk()
    report_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[ReportStatus | None] = mapped_column(
        pg_enum(ReportStatus, "report_status")
    )  # 최초 접수 시 null
    to_status: Mapped[ReportStatus] = mapped_column(
        pg_enum(ReportStatus, "report_status"), nullable=False
    )
    memo: Mapped[str | None] = mapped_column(Text)
    # null = 시스템 자동 전이 (SLA 에스컬레이션 등)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("admins.id", ondelete="SET NULL")
    )
    changed_at: Mapped[datetime] = created_at()
