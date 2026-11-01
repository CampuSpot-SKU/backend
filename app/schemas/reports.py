"""사용자용 신고 조회 응답 형식 — 명세서 5-1 "GET /reports/{display_no}", "GET /reports/mine" (작업 1-12).

학생 화면에 보여줄 것만 담는다: 관리자 메모(memo)·처리한 관리자(changed_by)는 내보내지 않음
(메모는 내부 협의 내용이 들어갈 수 있어서). 위치·신고 내용·담당 부서·처리 기한은 본인 확인이 된
조회에서만 보여준다(11/1 — 진행 상황 추적).
"""
from datetime import datetime

from pydantic import BaseModel

from app.models.enums import Priority, ReportStatus
from app.schemas.admin import NamedRef


class MyReportHistoryItem(BaseModel):
    from_status: ReportStatus | None  # 최초 접수는 null
    to_status: ReportStatus
    changed_at: datetime


class MyReport(BaseModel):
    display_no: int
    category: NamedRef
    priority: Priority
    status: ReportStatus
    created_at: datetime
    location: str | None = None
    description: str | None = None
    assigned_dept: str | None = None
    sla_deadline: datetime | None = None
    status_history: list[MyReportHistoryItem]  # 오래된 것부터


class MyReportSummary(BaseModel):
    display_no: int
    category: NamedRef
    priority: Priority
    status: ReportStatus
    created_at: datetime
    location: str | None = None


class MyReportList(BaseModel):
    reports: list[MyReportSummary]
