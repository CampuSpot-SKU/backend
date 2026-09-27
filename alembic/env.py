"""Alembic 실행 환경.

- DB URL은 app.config의 DATABASE_URL을 그대로 사용 (alembic.ini에 URL을 두지 않음 —
  비밀번호에 %가 들어가면 ini 보간 오류가 나고, 시크릿이 파일에 남을 위험도 있음).
- target_metadata는 app.models를 import해서 등록된 모델 전체를 대상으로 함.
"""
from logging.config import fileConfig

from sqlalchemy import create_engine, pool

import app.models  # noqa: F401  — 모델 등록용 import
from alembic import context
from app.config import get_settings
from app.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    url = get_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL이 설정되지 않았습니다.")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, compare_type=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
