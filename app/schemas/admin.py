"""관리자 API 요청/응답 형식 — 명세서 5-1 "관리자용 — 인증", "관리자용 — 신고 관리".

명세 필드 + 대시보드에 꼭 필요한 추가 필드(floor/detail/location_raw/sla_status)만 덧붙임.
추가 필드는 이름만 더한 것이라 명세 필드와 충돌 없음 (status.md 4장 제안 참고).
"""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.enums import ChatRole, ClusterStatus, Priority, ReportStatus

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


class ReportClusterRef(BaseModel):
    """이 신고가 묶인 반복 신고(기각된 묶음 제외) — 목록에서 같은 건끼리 모아 보여주는 데 씀."""

    id: uuid.UUID
    report_count: int  # 그 묶음에 묶인 신고 전체 수
    status: ClusterStatus
    whole_building: bool = False  # 호실이 달라도 건물 전체 원인으로 묶인 건


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
    description: str
    cluster: ReportClusterRef | None = None


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
    photo_url: str | None
    assigned_dept: str | None
    updated_at: datetime
    status_history: list[StatusHistoryItem]
    cluster_reports: list[AdminReportItem] = []  # 같은 묶음의 신고 전체(자기 자신 포함), 접수 순


class ReportSummary(BaseModel):
    """목록 위 숫자 띠 — 전체 신고 기준 (목록 limit과 무관)."""

    total: int
    by_status: dict[str, int]  # 접수·배정·처리중·해결·종료 모두 키로 존재
    open: int  # 접수+배정+처리중
    sla_over: int
    sla_soon: int
    urgent_open: int  # 처리 중인 P1


class SimilarReport(BaseModel):
    """유사 사례 한 건 — 같은 카테고리의 해결·종료된 과거 신고 (1-8c)."""

    id: uuid.UUID
    display_no: int
    category: NamedRef
    building: NamedRef | None
    floor: str | None
    detail: str | None
    location_raw: str | None
    description: str
    status: ReportStatus
    created_at: datetime
    resolved_at: datetime | None  # 마지막으로 '해결'된 시각
    resolution_memo: str | None  # 그때 남긴 처리 메모
    distance: float  # 설명 임베딩 코사인 거리 (0에 가까울수록 비슷), 소수 3자리


class SimilarReportList(BaseModel):
    embedding_ready: bool  # false = 이 신고의 임베딩이 아직 없음(접수 다음 날 탐지 배치가 채움) → items 빈 목록
    items: list[SimilarReport]  # 가까운 순, 최대 limit건


class ConversationMessage(BaseModel):
    """신고 대화의 한 줄 — 글과 시각만 (session_id·intent_scores·debug_payload는 내보내지 않음, 익명 원칙)."""

    role: ChatRole  # user = 학생, assistant = 챗봇
    content: str
    created_at: datetime


class ReportConversation(BaseModel):
    """상세보기 신고 대화 열람 (1-25) — 신고가 접수되기까지의 대화, 오래된 순. 대화가 없으면 messages=[]."""

    report_id: uuid.UUID
    display_no: int
    messages: list[ConversationMessage]
    truncated: bool  # true = 80개를 넘어 최근 쪽만 남김


class StatusChangeIn(BaseModel):
    to_status: ReportStatus
    memo: str | None = Field(default=None, max_length=1000, examples=["시설팀 배정, 오후 방문 예정"])


# ── 탐지·예측 (작업 1-8 — 명세서 3-3, 5-1 "관리자용 — 탐지·예측", 화면 계약 작업 1-8 지시서) ──


class ProblemClusterItem(BaseModel):
    """반복 신고 묶음 한 건 — 목록 원소이자 PATCH 응답 (같은 모양)."""

    id: uuid.UUID
    building: NamedRef | None  # 건물 없이 묶인 후보면 null
    detail: str | None
    category: NamedRef
    report_count: int  # 이 묶음에 묶인 신고 수 (problem_cluster_reports)
    open_count: int  # 그중 아직 해결 전(접수·배정·처리중)인 신고 수
    first_reported_at: datetime | None
    last_reported_at: datetime | None
    detected_at: datetime
    status: ClusterStatus


class ProblemClusterList(BaseModel):
    items: list[ProblemClusterItem]


class ProblemClusterDetail(ProblemClusterItem):
    """묶음 한 건 + 묶인 신고 전체 (접수 순)."""

    reports: list[AdminReportItem]


class ClusterAdvanceIn(BaseModel):
    to_status: Literal["배정", "처리중", "해결"]
    memo: str | None = Field(default=None, max_length=1000)


class ClusterAdvanceOut(ProblemClusterDetail):
    changed_count: int  # 상태가 실제로 바뀐 신고 수


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
    last_occurred_at: datetime | None = None


class PredictionList(BaseModel):
    items: list[PredictionItem]
