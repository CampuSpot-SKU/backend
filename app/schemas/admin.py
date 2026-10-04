"""관리자 API 요청/응답 형식 — 명세서 5-1 "관리자용 — 인증", "관리자용 — 신고 관리".

명세 필드 + 대시보드에 꼭 필요한 추가 필드(floor/detail/location_raw/sla_status)만 덧붙임.
추가 필드는 이름만 더한 것이라 명세 필드와 충돌 없음 (status.md 4장 제안 참고).
"""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.enums import ClusterStatus, Priority, ReportStatus

# SLA 상태: 처리 중인 건만 계산 (해결·종료는 null)
#   초과 = 마감 지남 / 임박 = SLA 시간 절반 이상 경과(명세 3-1 "50% 경과 → 리마인드"와 같은 기준) / 온타임 = 그 외
SlaStatus = Literal["온타임", "임박", "초과"]
SortKey = Literal["-created_at", "created_at", "sla_deadline", "priority"]


class LoginIn(BaseModel):
    login_id: str = Field(min_length=1, max_length=50, examples=["admin1"])
    password: str = Field(min_length=1, max_length=100)


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class NamedRef(BaseModel):
    id: uuid.UUID
    name: str


class AdminReportItem(BaseModel):
    """접수 목록(큐) 한 줄."""

    id: uuid.UUID
    display_no: int
    category: NamedRef
    priority: Priority
    status: ReportStatus
    building: NamedRef | None  # 건물 목록에서 매칭 안 되면 null → location_raw 참고
    floor: str | None
    detail: str | None
    location_raw: str | None
    sla_deadline: datetime | None
    sla_status: SlaStatus | None
    created_at: datetime


class AdminReportList(BaseModel):
    items: list[AdminReportItem]
    total: int  # 필터 적용 후 전체 건수 (limit/offset과 무관)


class StatusHistoryItem(BaseModel):
    from_status: ReportStatus | None  # 최초 접수는 null
    to_status: ReportStatus
    memo: str | None
    changed_by: NamedRef | None  # null = 시스템(챗봇 접수, SLA 자동전이 등)
    changed_at: datetime


class AdminReportDetail(AdminReportItem):
    """상세보기 — 신고 전체 필드 + 상태 이력."""

    category_id: uuid.UUID
    building_id: uuid.UUID | None
    description: str
    photo_url: str | None
    assigned_dept: str | None
    updated_at: datetime
    status_history: list[StatusHistoryItem]


class StatusChangeIn(BaseModel):
    to_status: ReportStatus
    memo: str | None = Field(default=None, max_length=1000, examples=["시설팀 배정, 오후 방문 예정"])


# ── 탐지·예측 (작업 1-8 — 명세서 3-3, 5-1 "관리자용 — 탐지·예측", 화면 계약 team-docs briefs/1-8.md) ──


class ProblemClusterItem(BaseModel):
    """문제 후보 한 건 — 목록 원소이자 PATCH 응답 (같은 모양)."""

    id: uuid.UUID
    building: NamedRef | None  # 건물 없이 묶인 후보면 null
    detail: str | None
    category: NamedRef
    report_count: int  # 이 후보에 묶인 신고 수 (problem_cluster_reports)
    detected_at: datetime
    status: ClusterStatus


class ProblemClusterList(BaseModel):
    items: list[ProblemClusterItem]


class ClusterStatusChangeIn(BaseModel):
    # 후보로 되돌리기는 없음 — 결정은 후보 → 승격/기각 한 번 (routers/detection.py)
    status: Literal["승격", "기각"]


class PredictionItem(BaseModel):
    """재발 예측 한 줄 — 배치가 계산을 끝낸 행만 나옴 (avg·예상 시점이 비어 있는 행은 제외)."""

    building: NamedRef | None
    detail: str | None
    category: NamedRef
    avg_recurrence_days: float
    predicted_next_at: datetime


class PredictionList(BaseModel):
    items: list[PredictionItem]
