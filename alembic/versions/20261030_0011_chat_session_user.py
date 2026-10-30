"""chat_sessions.user_id 추가 — 로그인 계정 기준 이전 대화 조회 (1-27)

명세서 5장·5-1: 로그인한 학생이 `claim`한 대화 세션만 계정에 연결한다(비로그인 세션은 NULL 그대로).
- 계정을 지우면 세션은 남기고 연결만 끊음(ON DELETE SET NULL) — 신고 조회는 session_id로 하므로 영향 없음.
- 목록 조회용 인덱스 (user_id, started_at).
- 기존 테이블에 null 허용 칼럼만 더하므로 지금 배포된 코드에 영향 없음. RLS는 0003에서 켜진 그대로.
- 이미 있으면 건너뜀(IF NOT EXISTS) — 두 번 실행해도 안전.

Revision ID: 0011_chat_session_user
Revises: 0010_users
Create Date: 2026-10-30
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0011_chat_session_user"
down_revision: str | None = "0010_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS user_id UUID "
        "REFERENCES users(id) ON DELETE SET NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_sessions_user_started ON chat_sessions (user_id, started_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chat_sessions_user_started")
    op.execute("ALTER TABLE chat_sessions DROP COLUMN IF EXISTS user_id")
