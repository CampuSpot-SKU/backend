"""사용자용 신고 상태 조회 (GET /reports/{display_no}) — TODO (Phase 1-3)"""
from fastapi import APIRouter

router = APIRouter(prefix="/reports", tags=["reports"])
