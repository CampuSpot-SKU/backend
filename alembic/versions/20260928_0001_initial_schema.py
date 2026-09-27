"""initial schema — 명세서 5장 테이블 16개 + ENUM 타입 + pgvector 확장

작성 방식: 모델(app/models) 기준으로 autogenerate한 뒤 사람이 검토·수정한 파일.

Revision ID: 0001_initial_schema
Revises: 
Create Date: 2026-09-28 01:12:47.294382
"""
from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '0001_initial_schema'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # RAG 임베딩용 벡터 타입 (Supabase에선 이미 켜져 있으면 아무 일도 안 함)
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table('admin_reg_documents',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('doc_type', sa.Enum('학칙', '공지', name='doc_type'), nullable=False),
    sa.Column('article_no', sa.String(length=50), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('source_url', sa.Text(), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_admin_reg_documents')),
    sa.UniqueConstraint('source_url', name=op.f('uq_admin_reg_documents_source_url'))
    )
    op.create_table('admins',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('login_id', sa.String(length=50), nullable=False),
    sa.Column('password_hash', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('dept', sa.String(length=100), nullable=True),
    sa.Column('role', sa.String(length=30), server_default=sa.text("'admin'"), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_admins')),
    sa.UniqueConstraint('login_id', name=op.f('uq_admins_login_id'))
    )
    op.create_table('buildings',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('aliases', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_buildings')),
    sa.UniqueConstraint('name', name=op.f('uq_buildings_name'))
    )
    op.create_table('categories',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_categories')),
    sa.UniqueConstraint('name', name=op.f('uq_categories_name'))
    )
    op.create_table('chat_sessions',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_identifier', sa.String(length=100), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_chat_sessions'))
    )
    op.create_index(op.f('ix_chat_sessions_user_identifier'), 'chat_sessions', ['user_identifier'], unique=False)
    op.create_table('detection_config',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('threshold_count', sa.Integer(), server_default='3', nullable=False),
    sa.Column('threshold_hours', sa.Integer(), server_default='72', nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_detection_config'))
    )
    op.create_table('priority_matrix_rules',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('impact', sa.Enum('고', '저', name='level'), nullable=False),
    sa.Column('urgency', sa.Enum('고', '저', name='level'), nullable=False),
    sa.Column('resulting_priority', sa.Enum('P1', 'P2', 'P3', 'P4', name='priority'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_priority_matrix_rules')),
    sa.UniqueConstraint('impact', 'urgency', name=op.f('uq_priority_matrix_rules_impact'))
    )
    op.create_table('sla_config',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('priority', sa.Enum('P1', 'P2', 'P3', 'P4', name='priority'), nullable=False),
    sa.Column('sla_hours', sa.Integer(), nullable=False),
    sa.Column('escalation_50pct_action', sa.Text(), nullable=True),
    sa.Column('escalation_100pct_action', sa.Text(), nullable=True),
    sa.Column('escalation_150pct_action', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sla_config')),
    sa.UniqueConstraint('priority', name=op.f('uq_sla_config_priority'))
    )
    op.create_table('admin_faq_embeddings',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('embedding', Vector(768), nullable=False),
    sa.Column('chunk_text', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['admin_reg_documents.id'], name=op.f('fk_admin_faq_embeddings_document_id_admin_reg_documents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_admin_faq_embeddings'))
    )
    op.create_index(op.f('ix_admin_faq_embeddings_document_id'), 'admin_faq_embeddings', ['document_id'], unique=False)
    op.create_index('ix_admin_faq_embeddings_embedding', 'admin_faq_embeddings', ['embedding'], unique=False, postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.create_table('chat_messages',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=False),
    sa.Column('role', sa.Enum('user', 'assistant', name='chat_role'), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('intent', sa.Enum('신고', '문의', '애매함', name='chat_intent'), nullable=True),
    sa.Column('intent_scores', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('debug_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['chat_sessions.id'], name=op.f('fk_chat_messages_session_id_chat_sessions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_chat_messages'))
    )
    op.create_index('ix_chat_messages_session_created', 'chat_messages', ['session_id', 'created_at'], unique=False)
    op.create_table('prediction_stats',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('building_id', sa.UUID(), nullable=True),
    sa.Column('detail', sa.String(length=100), nullable=True),
    sa.Column('category_id', sa.UUID(), nullable=False),
    sa.Column('avg_recurrence_days', sa.Float(), nullable=True),
    sa.Column('last_occurred_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('predicted_next_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['building_id'], ['buildings.id'], name=op.f('fk_prediction_stats_building_id_buildings')),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], name=op.f('fk_prediction_stats_category_id_categories')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_prediction_stats'))
    )
    op.create_index('ix_prediction_stats_location', 'prediction_stats', ['building_id', 'category_id', 'detail'], unique=False)
    op.create_table('problem_clusters',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('building_id', sa.UUID(), nullable=True),
    sa.Column('detail', sa.String(length=100), nullable=True),
    sa.Column('category_id', sa.UUID(), nullable=False),
    sa.Column('detected_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('status', sa.Enum('후보', '승격', '기각', name='cluster_status'), server_default='후보', nullable=False),
    sa.ForeignKeyConstraint(['building_id'], ['buildings.id'], name=op.f('fk_problem_clusters_building_id_buildings')),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], name=op.f('fk_problem_clusters_category_id_categories')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_problem_clusters'))
    )
    op.create_table('reports',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('display_no', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=True),
    sa.Column('category_id', sa.UUID(), nullable=False),
    sa.Column('priority', sa.Enum('P1', 'P2', 'P3', 'P4', name='priority'), nullable=False),
    sa.Column('status', sa.Enum('접수', '배정', '처리중', '해결', '종료', name='report_status'), server_default='접수', nullable=False),
    sa.Column('building_id', sa.UUID(), nullable=True),
    sa.Column('floor', sa.String(length=20), nullable=True),
    sa.Column('detail', sa.String(length=100), nullable=True),
    sa.Column('location_raw', sa.Text(), nullable=True),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('photo_url', sa.Text(), nullable=True),
    sa.Column('assigned_dept', sa.String(length=100), nullable=True),
    sa.Column('sla_deadline', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['building_id'], ['buildings.id'], name=op.f('fk_reports_building_id_buildings')),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], name=op.f('fk_reports_category_id_categories')),
    sa.ForeignKeyConstraint(['session_id'], ['chat_sessions.id'], name=op.f('fk_reports_session_id_chat_sessions'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reports')),
    sa.UniqueConstraint('display_no', name=op.f('uq_reports_display_no'))
    )
    op.create_index('ix_reports_building_category_created', 'reports', ['building_id', 'category_id', 'created_at'], unique=False)
    op.create_index('ix_reports_created_at', 'reports', ['created_at'], unique=False)
    op.create_index('ix_reports_status', 'reports', ['status'], unique=False)
    op.create_table('problem_cluster_reports',
    sa.Column('cluster_id', sa.UUID(), nullable=False),
    sa.Column('report_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['cluster_id'], ['problem_clusters.id'], name=op.f('fk_problem_cluster_reports_cluster_id_problem_clusters'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], name=op.f('fk_problem_cluster_reports_report_id_reports'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('cluster_id', 'report_id', name=op.f('pk_problem_cluster_reports'))
    )
    op.create_table('report_status_history',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('report_id', sa.UUID(), nullable=False),
    sa.Column('from_status', sa.Enum('접수', '배정', '처리중', '해결', '종료', name='report_status'), nullable=True),
    sa.Column('to_status', sa.Enum('접수', '배정', '처리중', '해결', '종료', name='report_status'), nullable=False),
    sa.Column('memo', sa.Text(), nullable=True),
    sa.Column('changed_by', sa.UUID(), nullable=True),
    sa.Column('changed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['changed_by'], ['admins.id'], name=op.f('fk_report_status_history_changed_by_admins'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], name=op.f('fk_report_status_history_report_id_reports'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_report_status_history'))
    )
    op.create_index(op.f('ix_report_status_history_report_id'), 'report_status_history', ['report_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_report_status_history_report_id'), table_name='report_status_history')
    op.drop_table('report_status_history')
    op.drop_table('problem_cluster_reports')
    op.drop_index('ix_reports_status', table_name='reports')
    op.drop_index('ix_reports_created_at', table_name='reports')
    op.drop_index('ix_reports_building_category_created', table_name='reports')
    op.drop_table('reports')
    op.drop_table('problem_clusters')
    op.drop_index('ix_prediction_stats_location', table_name='prediction_stats')
    op.drop_table('prediction_stats')
    op.drop_index('ix_chat_messages_session_created', table_name='chat_messages')
    op.drop_table('chat_messages')
    op.drop_index('ix_admin_faq_embeddings_embedding', table_name='admin_faq_embeddings', postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.drop_index(op.f('ix_admin_faq_embeddings_document_id'), table_name='admin_faq_embeddings')
    op.drop_table('admin_faq_embeddings')
    op.drop_table('sla_config')
    op.drop_table('priority_matrix_rules')
    op.drop_table('detection_config')
    op.drop_index(op.f('ix_chat_sessions_user_identifier'), table_name='chat_sessions')
    op.drop_table('chat_sessions')
    op.drop_table('categories')
    op.drop_table('buildings')
    op.drop_table('admins')
    op.drop_table('admin_reg_documents')

    # create_table이 자동으로 만든 ENUM 타입은 drop_table로 안 지워지므로 직접 삭제
    for enum_name in (
        "report_status", "priority", "level", "chat_role", "chat_intent", "doc_type",
        "cluster_status",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
    # vector 확장은 다른 곳에서 쓸 수도 있어서 지우지 않음
