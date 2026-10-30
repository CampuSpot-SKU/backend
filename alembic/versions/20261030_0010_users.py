"""users 테이블 추가 — 학생 회원가입·로그인 (1-24)

명세서 5장·5-1: 선택 사항인 학생 계정. 아이디(UNIQUE)·bcrypt 해시·이름만 저장하고 다른 테이블과 연결하지 않음
(신고·대화는 계정과 무관하게 익명 그대로).
- 새 테이블이라 RLS를 켠다(0003 규칙 — Supabase 자동 REST API로 밖에 노출되지 않게).
- 이미 있으면 건너뜀(IF NOT EXISTS) — 두 번 실행해도 안전.

Revision ID: 0010_users
Revises: 0009_reports_escalation_level
Create Date: 2026-10-30
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0010_users"
down_revision: str | None = "0009_reports_escalation_level"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            login_id VARCHAR(20) NOT NULL UNIQUE,
            password_hash VARCHAR(100) NOT NULL,
            name VARCHAR(20) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS users")
