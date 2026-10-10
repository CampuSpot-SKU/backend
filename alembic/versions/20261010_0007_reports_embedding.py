"""reports.embedding 칼럼 추가 — 신고 설명 임베딩 (1-8b)

명세서 3-3·5장: 같은 건물·카테고리에서 세부위치 표기가 달라도 설명이 비슷하면 같은 문제로 묶고(탐지),
상세보기의 유사 사례 검색에도 같은 임베딩을 쓴다.
- vector(768), NULL 허용 — 기존 신고와 새 신고는 NULL로 시작한다. 접수 경로(backend)는 이 칼럼을 쓰지 않고,
  ai 탐지 배치가 매일 비어 있는 신고만 채운다(접수 지연·Gemini 장애 영향 없음).
- 이미 있으면 건너뜀(IF NOT EXISTS) — 두 번 실행해도 안전. RLS는 테이블 단위라 따로 할 일 없음.
- 인덱스는 만들지 않는다: 건물·카테고리로 먼저 좁힌 몇십~몇백 건끼리만 비교해 순차 스캔으로 충분하다.
  유사 사례 검색이 전체 신고를 훑게 되면 그때 HNSW 인덱스를 따로 추가한다.

Revision ID: 0007_reports_embedding
Revises: 0006_chat_message_sources
Create Date: 2026-10-10
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0007_reports_embedding"
down_revision: str | None = "0006_chat_message_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS embedding vector(768)")


def downgrade() -> None:
    op.execute("ALTER TABLE reports DROP COLUMN IF EXISTS embedding")
