"""doc_type ENUM에 '안내' 추가 — 학교 홈페이지 안내 페이지(1-4a) RAG용

명세서 5장 admin_reg_documents.doc_type: 학칙 / 공지 / 안내.
- 이미 값이 있으면 건너뜀 (IF NOT EXISTS) — 두 번 실행해도 안전.
- PostgreSQL은 ENUM 값을 지울 수 없어서 downgrade는 아무것도 하지 않음.
  ('안내' 문서가 들어 있을 수 있으므로 억지로 지우지 않는다.)

Revision ID: 0005_add_doc_type_guide
Revises: 0004_seed_buildings
Create Date: 2026-10-06
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0005_add_doc_type_guide"
down_revision: str | None = "0004_seed_buildings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE doc_type ADD VALUE IF NOT EXISTS '안내'")


def downgrade() -> None:
    # ENUM 값은 제거할 수 없음 — 의도적으로 비워 둠.
    pass
