"""탐지(반복 문제 클러스터)와 예측(재발 통계) — ai 서비스 배치가 채움."""
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, String, Table
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models._common import created_at, pg_enum, uuid_pk
from app.models.enums import ClusterStatus


class ProblemCluster(Base):
    __tablename__ = "problem_clusters"

    id: Mapped[uuid.UUID] = uuid_pk()
    building_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("buildings.id"))
    detail: Mapped[str | None] = mapped_column(String(100))
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("categories.id"), nullable=False)
    detected_at: Mapped[datetime] = created_at()
    status: Mapped[ClusterStatus] = mapped_column(
        pg_enum(ClusterStatus, "cluster_status"),
        nullable=False,
        server_default=ClusterStatus.CANDIDATE.value,
    )


# 클러스터-신고 N:M 매핑
problem_cluster_reports = Table(
    "problem_cluster_reports",
    Base.metadata,
    Column(
        "cluster_id", ForeignKey("problem_clusters.id", ondelete="CASCADE"), primary_key=True
    ),
    Column("report_id", ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True),
)


class PredictionStat(Base):
    __tablename__ = "prediction_stats"
    __table_args__ = (
        Index("ix_prediction_stats_location", "building_id", "category_id", "detail"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    building_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("buildings.id"))
    detail: Mapped[str | None] = mapped_column(String(100))
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("categories.id"), nullable=False)
    avg_recurrence_days: Mapped[float | None] = mapped_column(Float)
    last_occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    predicted_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
