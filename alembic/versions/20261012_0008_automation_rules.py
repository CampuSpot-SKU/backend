"""automation_rules 테이블 추가 — 조건-액션 규칙 (1-21)

명세서 3-1·5장·5-1: 카테고리(필수)와 건물(비우면 모든 건물)이 맞으면 매트릭스 결과 대신 정한 우선순위로 접수.
- 같은 (카테고리, 건물) 조합은 하나만 — 건물 NULL끼리도 겹치지 않게 NULLS NOT DISTINCT(PostgreSQL 15+, Supabase OK).
- 건물을 설정에서 지우면 그 건물 규칙도 같이 지움(ON DELETE CASCADE). 카테고리는 지우지 않고 끄기만 하므로 그대로.
- 새 테이블이라 RLS를 켠다(0003 규칙 — Supabase 자동 REST API로 밖에 노출되지 않게).
- 이미 있으면 건너뜀(IF NOT EXISTS) — 두 번 실행해도 안전.

Revision ID: 0008_automation_rules
Revises: 0007_reports_embedding
Create Date: 2026-10-12
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0008_automation_rules"
down_revision: str | None = "0007_reports_embedding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS automation_rules (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name VARCHAR(50) NOT NULL UNIQUE,
            category_id UUID NOT NULL REFERENCES categories(id),
            building_id UUID REFERENCES buildings(id) ON DELETE CASCADE,
            resulting_priority priority NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_automation_rules_target "
        "ON automation_rules (category_id, building_id) NULLS NOT DISTINCT"
    )
    op.execute("ALTER TABLE automation_rules ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS automation_rules")
