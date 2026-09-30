"""seed — 건물 12개 (학교 캠퍼스맵 공식 목록, 생활관 제외)

명세서 4-1 "위치 처리 규칙", team-docs/data/campus/buildings.json 기준.
- aliases는 비워 둠. 특히 "은주관"은 은주1관·은주2관 중 어느 쪽 별칭으로도 넣지 않는다 —
  슬롯필링이 "은주1관인가요, 은주2관인가요?"라고 되묻기 때문 (app/services/slot_filling.py).
- 이미 같은 이름이 있으면 건너뜀 (ON CONFLICT DO NOTHING) — 운영 DB에서 먼저 손으로 넣었어도 안전.

Revision ID: 0004_seed_buildings
Revises: 0003_enable_rls
Create Date: 2026-09-30
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0004_seed_buildings"
down_revision: str | None = "0003_enable_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BUILDINGS = [
    "대일관", "문예관", "본관", "북악관", "상승관", "유담관",
    "은주1관", "은주2관", "청운관", "한림관", "혜인관", "수인관",
]


def upgrade() -> None:
    for name in BUILDINGS:
        op.execute(f"INSERT INTO buildings (name) VALUES ('{name}') ON CONFLICT (name) DO NOTHING")


def downgrade() -> None:
    # 이미 신고(reports)가 참조 중인 건물은 지울 수 없으므로, 참조 없는 것만 지움
    names = ", ".join(f"'{n}'" for n in BUILDINGS)
    op.execute(
        f"DELETE FROM buildings WHERE name IN ({names}) "
        "AND id NOT IN (SELECT building_id FROM reports WHERE building_id IS NOT NULL)"
    )
