"""seed — 설정 테이블 초기값 + 관리자 계정

- categories: 명세서 3-1 카테고리 6개
- priority_matrix_rules: 영향도×긴급도 → P1~P4 (명세서 3-1)
- sla_config: P1 4h / P2 24h / P3 72h / P4 168h (2026-09-28 결정), 에스컬레이션 단계는 명세서 3-1
- detection_config: 72시간 내 3건 (명세서 3-3)
- admins: admin1~admin5, 비밀번호는 bcrypt 해시만 저장 (평문은 팀 채널로 공유)

값은 전부 나중에 DB에서 직접 수정 가능 (설정 테이블로 분리한 이유).

Revision ID: 0002_seed_config_and_admins
Revises: 0001_initial_schema
Create Date: 2026-09-28
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0002_seed_config_and_admins"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATEGORIES = ["전기", "시설·설비", "청소·위생", "안전", "IT·네트워크", "기타"]

# (영향도, 긴급도, 우선순위)
PRIORITY_MATRIX = [("고", "고", "P1"), ("고", "저", "P2"), ("저", "고", "P3"), ("저", "저", "P4")]

SLA_HOURS = {"P1": 4, "P2": 24, "P3": 72, "P4": 168}
ESCALATION = ("리마인드", "부서장 알림 + 우선순위 상향", "관리자 긴급알림")

# bcrypt(rounds=12) 해시 — 5개 계정 공통 초기 비밀번호
ADMIN_PASSWORD_HASH = "$2b$12$BQTWjNjES/uPe.bbs6XiaON1SPNSV09yeE6q2k7hnsceZ0FWIFDX2"
ADMIN_COUNT = 5


def upgrade() -> None:
    for name in CATEGORIES:
        op.execute(f"INSERT INTO categories (name) VALUES ('{name}')")

    for impact, urgency, priority in PRIORITY_MATRIX:
        op.execute(
            "INSERT INTO priority_matrix_rules (impact, urgency, resulting_priority) "
            f"VALUES ('{impact}', '{urgency}', '{priority}')"
        )

    for priority, hours in SLA_HOURS.items():
        op.execute(
            "INSERT INTO sla_config (priority, sla_hours, escalation_50pct_action, "
            "escalation_100pct_action, escalation_150pct_action) "
            f"VALUES ('{priority}', {hours}, '{ESCALATION[0]}', '{ESCALATION[1]}', "
            f"'{ESCALATION[2]}')"
        )

    op.execute("INSERT INTO detection_config (threshold_count, threshold_hours) VALUES (3, 72)")

    for i in range(1, ADMIN_COUNT + 1):
        op.execute(
            "INSERT INTO admins (login_id, password_hash, name) "
            f"VALUES ('admin{i}', '{ADMIN_PASSWORD_HASH}', '관리자{i}')"
        )


def downgrade() -> None:
    op.execute("DELETE FROM admins WHERE login_id LIKE 'admin_'")
    op.execute("DELETE FROM detection_config")
    op.execute("DELETE FROM sla_config")
    op.execute("DELETE FROM priority_matrix_rules")
    names = ", ".join(f"'{n}'" for n in CATEGORIES)
    op.execute(f"DELETE FROM categories WHERE name IN ({names})")
