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

    # 기타
    cron_secret: str = ""
    sentry_dsn: str = ""
    rate_limit_per_minute: int = 20

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
