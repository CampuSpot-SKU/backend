"""FastAPI 진입점 — /healthz 헬스체크, /api/v1 라우터 등록.
라우터(chat/reports/admin/cron)는 아직 구현 전(TODO)이라 실제 등록은 각 라우터에
APIRouter가 생기는 시점에 추가함. 지금은 컨테이너가 정상 기동하도록 최소 앱만 둠.
"""
from fastapi import FastAPI

app = FastAPI(title="campuspot-backend")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
