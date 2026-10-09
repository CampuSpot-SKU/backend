"""chat_messages.sources 칼럼 추가 — 행정 문의 답변의 근거 목록 (1-4c)

명세서 5-1: SSE done 이벤트 `sources: [{title, article_no, url?}]`와 문의 로그 `sources`가 같은 형식.
- NULL 허용 JSONB — 기존 메시지는 그대로 NULL(문의 로그에서 근거 없음으로 보임).
- 이미 있으면 건너뜀(IF NOT EXISTS) — 두 번 실행해도 안전. RLS는 테이블 단위라 따로 할 일 없음.

Revision ID: 0006_chat_message_sources
Revises: 0005_add_doc_type_guide
Create Date: 2026-10-09
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0006_chat_message_sources"
down_revision: str | None = "0005_add_doc_type_guide"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS sources JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE chat_messages DROP COLUMN IF EXISTS sources")
