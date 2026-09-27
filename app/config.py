"""환경변수 설정 — 모든 설정값은 여기서만 읽는다 (os.environ 직접 접근 금지).

Cloud Run에서는 deploy.yml의 --set-env-vars로, 로컬 도구(alembic 등)는 .env로 주입된다.
값이 비어 있어도 앱 import 자체는 실패하지 않게 기본값을 ""로 둠 — 실제 사용 시점에 검증.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache
def get_settings() -> Settings:
    return Settings()
