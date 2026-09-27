"""모든 ORM 모델의 부모 클래스.

naming_convention을 지정해두면 Alembic이 만드는 제약조건(PK/FK/UNIQUE 등) 이름이
항상 같은 규칙으로 생성돼서, 나중에 제약조건을 수정/삭제하는 마이그레이션이 깨지지 않음.
"""
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
