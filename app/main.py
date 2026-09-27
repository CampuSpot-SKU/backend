"""FastAPI 진입점 — /healthz 헬스체크 + /api/v1 라우터 등록.

구조:
  app/config.py          환경변수 설정 (get_settings)
  app/db/                DB 엔진/세션(session.py), ORM Base(base.py)
  app/models/            ORM 모델 — 새 모델은 models/__init__.py에 import 필수(Alembic 인식용)
  app/routers/           도메인별 라우터 — 전부 /api/v1 프리픽스로 등록
  app/middleware/        요청별 correlation ID
"""
from fastapi import FastAPI

from app.db.session import check_db
from app.middleware.request_id import RequestIdMiddleware, setup_logging
from app.routers import admin, chat, cron, reports

setup_logging()

app = FastAPI(title="campuspot-backend")
app.add_middleware(RequestIdMiddleware)

API_PREFIX = "/api/v1"
for r in (chat.router, reports.router, admin.router, cron.router):
    app.include_router(r, prefix=API_PREFIX)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """항상 200을 반환 — 컨테이너 생존 확인용이라 DB 장애로 인스턴스가 죽지 않게 함.
    DB 상태는 db 필드로 따로 알려줌 (배포 후 DB 연결 확인용).
    """
    db_ok = check_db()
    return {"status": "ok" if db_ok else "degraded", "db": "ok" if db_ok else "error"}
