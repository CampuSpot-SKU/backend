"""관리자 행정 문의 로그 응답 형식 — 작업 1-18 (명세서 5-1 "문의 로그 API 형식", 10/8 확정).

학생 질문(role=user) 1개와 바로 뒤 챗봇 답변 1개를 한 줄로 묶는다.
session_id·user_identifier·debug_payload 원문은 내보내지 않는다(익명 원칙) — low_confidence 표시만.
"""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ChatLogItem(BaseModel):
    id: uuid.UUID  # 학생 질문 메시지 id
    asked_at: datetime
    question: str
    answer: str | None  # 바로 뒤 챗봇 답변 — 없으면(답변 실패 등) null
    intent: str | None
    intent_scores: dict[str, Any] | None  # 예: {"report_score": 5, "inquiry_score": 95, "safety_concern": false}
    low_confidence: bool
    sources: list[dict[str, Any]] | None  # 답변의 근거 [{title, article_no, url}] — 1-4c 이전 답변은 null


class ChatLogList(BaseModel):
    total: int  # 필터 적용 후 전체 질문 수
    items: list[ChatLogItem]
