"""라우터 공통 의존성 — Annotated 방식 (FastAPI 권장, ruff B008 대응).

사용 예:
    def list_reports(db: DbSession, admin: CurrentAdmin): ...
"""
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.admin import Admin
from app.services.auth import get_current_admin

DbSession = Annotated[Session, Depends(get_db)]
# 관리자 전용 엔드포인트에 넣으면 Authorization: Bearer 토큰을 검사하고 로그인한 관리자를 넘겨줌
CurrentAdmin = Annotated[Admin, Depends(get_current_admin)]
