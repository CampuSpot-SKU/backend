"""DB 엔진/세션 관리.

- 엔진은 처음 필요할 때 1번만 만든다(lazy). DATABASE_URL이 없는 환경(테스트 등)에서도
  앱 import가 깨지지 않게 하기 위함.
- Supabase Session pooler는 동시 커넥션 수가 제한적이고 ai 서비스와 같은 DB를 공유하므로
  인스턴스당 풀을 작게 잡는다 (pool_size 3 + overflow 2 = 최대 5).
- 라우터에서는 `db: Session = Depends(get_db)`로 받아서 쓴다.
"""
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    url = get_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL이 설정되지 않았습니다.")
    return create_engine(
        url,
        pool_size=3,
        max_overflow=2,
        pool_pre_ping=True,  # 끊긴 커넥션 자동 감지 (Cloud Run 유휴 후 재사용 대비)
        pool_recycle=300,
    )


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI 의존성 — 요청마다 세션 1개, 요청 끝나면 반드시 닫음."""
    db = get_sessionmaker()()
    try:
        yield db
    finally:
        db.close()


def check_db() -> bool:
    """/healthz용 — DB에 SELECT 1이 되는지만 확인."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — 어떤 DB 오류든 "연결 불가"로만 판단
        return False
