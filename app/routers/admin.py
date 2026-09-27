"""관리자용 엔드포인트 (인증/신고관리/탐지·예측/설정) — 명세서 5-1 참고. TODO (Phase 1-6)"""
from fastapi import APIRouter

router = APIRouter(prefix="/admin", tags=["admin"])
