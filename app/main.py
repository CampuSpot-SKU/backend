"""FastAPI 진입점 — /health 헬스체크 + CORS + /api/v1 라우터 등록.

구조:
  app/config.py          환경변수 설정 (get_settings)
  app/db/                DB 엔진/세션(session.py), ORM Base(base.py)
  app/models/            ORM 모델 — 새 모델은 models/__init__.py에 import 필수(Alembic 인식용)
  app/routers/           도메인별 라우터 — 전부 /api/v1 프리픽스로 등록 (요청/응답 처리만)
  app/schemas/           요청/응답 Pydantic 모델 (/docs에 자동 반영)
  app/services/          판단·생성 로직 (슬롯필링, 신고 접수, ai 서비스 호출)
  app/rate_limit.py      챗봇 세션당 요청 제한 (slowapi)
  app/middleware/        요청별 correlation ID
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded

from app.db.session import check_db
from app.middleware.request_id import RequestIdMiddleware, setup_logging
from app.rate_limit import limiter, rate_limit_exceeded_handler
from app.routers import admin, chat, config_admin, cron, detection, locations, photos, reports

setup_logging()

app = FastAPI(title="campuspot-backend")
app.add_middleware(RequestIdMiddleware)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# 프론트(다른 Cloud Run 주소)에서 브라우저로 직접 호출할 수 있게 허용 (명세서 D1 결정: CORS 방식).
# campuspot-frontend 서비스의 Cloud Run 주소 두 형식(프로젝트번호형·해시형)을 모두 허용 —
# URL을 코드에 복사해 넣을 필요 없음. 세션은 쿠키가 아니라 session_id를 주고받으므로 credentials 불필요.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https://campuspot-frontend-[a-z0-9-]+\.(asia-northeast3\.run\.app|a\.run\.app)$",
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

API_PREFIX = "/api/v1"
for r in (chat.router, photos.router, locations.router, reports.router, admin.router, detection.router, config_admin.router, cron.router):
    app.include_router(r, prefix=API_PREFIX)


# 주의: Cloud Run은 z로 끝나는 경로(/healthz 등)를 예약해서 외부에서 404가 남 → /health 사용
@app.get("/health")
def health() -> dict[str, str]:
    """항상 200을 반환 — 컨테이너 생존 확인용이라 DB 장애로 인스턴스가 죽지 않게 함.
    DB 상태는 db 필드로 따로 알려줌 (배포 후 DB 연결 확인용).
    """
    db_ok = check_db()
    return {"status": "ok" if db_ok else "degraded", "db": "ok" if db_ok else "error"}
