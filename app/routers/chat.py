"""챗봇 대화 — 신고 접수(슬롯필링) + 행정문의 라우팅.

의도분류/RAG는 ai 서비스에 HTTP 호출 (AI_SERVICE_URL, X-Internal-Secret: AI_SERVICE_SECRET).
엔드포인트: 명세서 5-1 "사용자용 — 챗봇" — TODO (Phase 1-2~4)
"""
from fastapi import APIRouter

router = APIRouter(prefix="/chat", tags=["chat"])
