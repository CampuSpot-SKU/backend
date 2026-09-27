"""enable RLS — Supabase 자동 REST API(PostgREST)로 테이블이 밖에 노출되는 것 차단

Supabase는 public 스키마 테이블을 anon/authenticated 역할로 REST API에 자동 공개한다.
RLS를 켜고 정책(policy)을 하나도 만들지 않으면 그 역할들은 아무 행도 읽고 쓸 수 없다.
우리 backend/ai는 테이블 소유자(postgres)로 직접 접속하므로 RLS 영향을 받지 않는다.

새 테이블을 추가하는 마이그레이션에서도 반드시 ENABLE ROW LEVEL SECURITY를 같이 넣을 것.

Revision ID: 0003_enable_rls
Revises: 0002_seed_config_and_admins
Create Date: 2026-09-28
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0003_enable_rls"
down_revision: str | None = "0002_seed_config_and_admins"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = [
    "admin_faq_embeddings",
    "admin_reg_documents",
    "admins",
    "alembic_version",
    "buildings",
    "categories",
    "chat_messages",
    "chat_sessions",
    "detection_config",
    "prediction_stats",
    "priority_matrix_rules",
    "problem_cluster_reports",
    "problem_clusters",
    "report_status_history",
    "reports",
    "sla_config",
]


def upgrade() -> None:
    for t in TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    for t in TABLES:
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
