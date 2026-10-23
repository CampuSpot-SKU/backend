"""환경변수 설정 — 모든 설정값은 여기서만 읽는다 (os.environ 직접 접근 금지).

Cloud Run에서는 deploy.yml의 --set-env-vars로, 로컬 도구(alembic 등)는 .env로 주입된다.
값이 비어 있어도 앱 import 자체는 실패하지 않게 기본값을 ""로 둠 — 실제 사용 시점에 검증.
"""
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# requirements.txt에 설치된 드라이버는 psycopg2뿐이라, 어떤 형태로 URL이 들어와도
# SQLAlchemy가 psycopg2를 쓰도록 스킴을 통일한다.
# (예: GitHub Secret이 postgresql+psycopg://로 등록돼 있으면 psycopg3를 찾다가 실패함)
_PG_SCHEMES = ("postgresql+psycopg2://", "postgresql+psycopg://", "postgresql://", "postgres://")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # DB (Supabase Session pooler, postgresql://...)
    database_url: str = ""
    supabase_url: str = ""
    supabase_service_key: str = ""

    # ai 서비스 호출
    ai_service_url: str = ""
    ai_service_secret: str = ""

    # 인증
    jwt_secret: str = ""
    jwt_expire_minutes: int = 60

    # 알림 메일 — SLA 에스컬레이션(1-13). Gmail SMTP, 비어 있으면 메일만 건너뜀(명세 6장·6-2 O-2)
    smtp_user: str = ""
    smtp_app_password: str = ""
    alert_email_to: str = ""  # 받는 주소, 쉼표로 여러 개
    # 알림 메일에 넣는 관리자 화면 주소
    admin_page_url: str = "https://campuspot-frontend-890230516680.asia-northeast3.run.app/admin"

    # 기타
    cron_secret: str = ""
    sentry_dsn: str = ""
    rate_limit_per_minute: int = 20  # 세션당 메시지·새로고침
    # 접속 IP당 (S-3) — slowapi 표기, 여러 개는 ";"로. 학교 와이파이 공유 IP를 고려해 넉넉히
    session_create_limit: str = "10/minute;100/hour"
    chat_ip_limit: str = "60/minute"

    @field_validator("database_url")
    @classmethod
    def _normalize_pg_scheme(cls, v: str) -> str:
        v = v.strip()
        for scheme in _PG_SCHEMES:
            if v.startswith(scheme):
                return "postgresql+psycopg2://" + v[len(scheme):]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
