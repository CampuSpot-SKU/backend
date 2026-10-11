"""reports.escalation_level 칼럼 추가 — SLA 에스컬레이션 진행 단계 (1-13)

명세서 3-1·5장: SLA 배치(`POST /cron/sla-check`)가 50%·100%·150% 단계마다 알림을 한 번씩만 보내도록
마지막으로 처리한 단계를 저장한다(0 = 아직 없음).
- SMALLINT NOT NULL DEFAULT 0 — 기존 신고는 0으로 채워짐. 첫 배치 실행 때 이미 지난 단계는 가장 높은 단계 알림
  한 번으로 묶여 나간다(밀린 단계를 하나씩 보내지 않음).
- 이미 있으면 건너뜀(IF NOT EXISTS) — 두 번 실행해도 안전. RLS는 테이블 단위라 따로 할 일 없음.

Revision ID: 0009_reports_escalation_level
Revises: 0008_automation_rules
Create Date: 2026-10-12
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0009_reports_escalation_level"
down_revision: str | None = "0008_automation_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS escalation_level SMALLINT NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE reports DROP COLUMN IF EXISTS escalation_level")
