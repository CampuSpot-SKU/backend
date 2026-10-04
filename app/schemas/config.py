"""관리자 설정 API 요청/응답 형식 — 명세서 5-1 "관리자용 — 설정 관리" (작업 1-17).

모든 PUT은 "전체를 통째로 보내 저장" — 응답은 저장 후 GET과 같은 모양.
형식 결정은 team-docs status.md 4장 (10/4 규민 제안).
"""
import uuid
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.models.enums import Level, Priority

CategoryName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
BuildingName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Alias = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
ActionText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


# ── 카테고리 ── (삭제 없음: 신고가 참조하므로 is_active로 끄기)
class CategoryItem(BaseModel):
    id: uuid.UUID
    name: str
    is_active: bool


class CategoryList(BaseModel):
    items: list[CategoryItem]


class CategoryIn(BaseModel):
    id: uuid.UUID | None = None  # 없으면 새로 추가
    name: CategoryName
    is_active: bool = True


class CategoryListIn(BaseModel):
    items: list[CategoryIn] = Field(min_length=1, max_length=50)


# ── 건물 ── (목록에서 빠진 건물은 삭제, 단 신고 등이 참조하면 409)
class BuildingItem(BaseModel):
    id: uuid.UUID
    name: str
    aliases: list[str]


class BuildingList(BaseModel):
    items: list[BuildingItem]


class BuildingIn(BaseModel):
    id: uuid.UUID | None = None  # 없으면 새로 추가
    name: BuildingName
    aliases: list[Alias] = Field(default_factory=list, max_length=30)


class BuildingListIn(BaseModel):
    items: list[BuildingIn] = Field(max_length=200)


# ── 우선순위 매트릭스 ── (영향도×긴급도 고/저 4칸, 결과만 바꿈)
class PriorityRuleItem(BaseModel):
    impact: Level
    urgency: Level
    resulting_priority: Priority


class PriorityMatrix(BaseModel):
    items: list[PriorityRuleItem]


class PriorityMatrixIn(BaseModel):
    items: list[PriorityRuleItem] = Field(min_length=4, max_length=4)  # 4칸이 정확히 한 번씩 (서비스에서 검사)


# ── SLA ── (P1~P4 한 줄씩. 바꿔도 이미 접수된 신고의 마감은 다시 계산하지 않음)
class SlaItem(BaseModel):
    priority: Priority
    sla_hours: int = Field(ge=1, le=8760)
    escalation_50pct_action: ActionText | None = None
    escalation_100pct_action: ActionText | None = None
    escalation_150pct_action: ActionText | None = None


class SlaList(BaseModel):
    items: list[SlaItem]


class SlaListIn(BaseModel):
    items: list[SlaItem] = Field(min_length=4, max_length=4)  # P1~P4가 정확히 한 번씩 (서비스에서 검사)


# ── 탐지 임계치 ── (객체 하나: threshold_hours 안에 threshold_count건 이상이면 문제 후보)
class DetectionSettings(BaseModel):
    threshold_count: int = Field(ge=2, le=100)
    threshold_hours: int = Field(ge=1, le=8760)
