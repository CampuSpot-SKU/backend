"""ORM 모델 모음.

새 모델 파일을 만들면 반드시 여기서 import할 것 — Alembic(env.py)은 이 패키지를
import해서 Base.metadata에 등록된 테이블만 인식한다.
"""
from app.db.base import Base

__all__ = ["Base"]
