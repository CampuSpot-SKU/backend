"""관리자 통계 응답 형식 — 작업 1-16 (명세서 4-2 "카테고리별 건수, 평균 처리시간, SLA 준수율").

명세 5-1에 형식이 없어 status.md 4장에 제안한 형식(2026-10-05 규민). 계산 기준은
services/stats_query.py 맨 위 주석 참고.
"""
from datetime import datetime

from pydantic import BaseModel

from app.models.enums import Priority, ReportStatus
from app.schemas.admin import NamedRef


class SlaSummary(BaseModel):
    """SLA 준수 — 결과가 정해진 건만 셈 (아직 마감 전인 처리 중 건·마감 없는 건은 제외)."""

    met: int  # 마감 안에 해결
    breached: int  # 마감 지나서 해결 + 처리 중인데 이미 마감 지남
    compliance_pct: float | None  # met / (met + breached) × 100, 소수 1자리. 셀 건이 없으면 null


class StatsGroup(BaseModel):
    """한 묶음(전체·카테고리·우선순위)의 지표."""

    count: int  # 기간 안에 접수된 신고 수
    resolved_count: int  # 그중 해결·종료된 수 (처리시간을 잴 수 있는 건만)
    avg_resolution_hours: float | None  # 접수 → 해결 평균 시간, 소수 1자리. 해결 건이 없으면 null
    sla: SlaSummary


class CategoryStats(StatsGroup):
    category: NamedRef
    is_active: bool  # 꺼진 카테고리도 기간 안 신고가 있으면 나옴


class PriorityStats(StatsGroup):
    priority: Priority


class StatusCount(BaseModel):
    status: ReportStatus
    count: int


class StatsPeriod(BaseModel):
    days: int | None  # 요청한 기간(일). null = 전체 기간
    since: datetime | None  # 이 시각 이후 접수된 신고만. null = 전체
    until: datetime  # 계산 시각 (SLA 초과 판정 기준)


class AdminStats(BaseModel):
    period: StatsPeriod
    total: StatsGroup
    by_status: list[StatusCount]  # 워크플로우 순서(접수→종료), 0건도 포함
    by_category: list[CategoryStats]  # 건수 많은 순 → 이름순. 켜진 카테고리는 0건도 포함
    by_priority: list[PriorityStats]  # P1→P4, 0건도 포함
