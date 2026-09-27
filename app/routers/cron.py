"""Cloud Scheduler 전용 — SLA/에스컬레이션 체크만 여기 남음 (reports 도메인).

반복탐지/재발예측/공지크롤링은 ai 서비스의 /api/v1/cron/* 로 이전됨.
POST /cron/sla-check (X-Cron-Secret 헤더 필요) — TODO
"""
from fastapi import APIRouter

router = APIRouter(prefix="/cron", tags=["cron"])
