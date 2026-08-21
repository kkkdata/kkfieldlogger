"""expand employee expression forbidden phrases

Revision ID: 20260616_0022
Revises: 20260615_0021
Create Date: 2026-06-16 00:15:00
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260616_0022"
down_revision = "20260615_0021"
branch_labels = None
depends_on = None


PHRASES = [
    ("employee_contribution_zh_v1:effort", "努力", "改为记录动作、记录覆盖或资料完整度。"),
    ("employee_contribution_zh_v1:praise", "值得肯定", "避免评价员工本人，改为说明记录对项目回看的帮助。"),
    ("employee_contribution_zh_v1:high_quality", "高质量", "除非事实中有质量审核结论，否则不要评价照片或工作质量。"),
    ("employee_contribution_zh_v1:photo_quality", "照片质量", "改为可识别信息、记录内容或现场信息。"),
    ("employee_contribution_zh_v1:performance_word", "表现", "避免员工表现评价，改为现场记录情况。"),
    ("employee_contribution_zh_v1:participation", "参与度", "避免人员参与度判断，改为记录数量或记录日。"),
    ("employee_contribution_zh_v1:outstanding", "出色", "避免夸奖式绩效表达。"),
    ("employee_contribution_zh_v1:excellent", "优秀", "避免夸奖式绩效表达。"),
    ("employee_contribution_zh_v1:keep_improving", "改进", "避免对员工提出绩效改进式建议，改为可补充记录类型。"),
    ("employee_contribution_zh_v1:efficient", "高效", "避免推断团队或个人效率。"),
]


def upgrade() -> None:
    table = sa.table(
        "expression_forbidden_phrases",
        sa.column("id", sa.String),
        sa.column("set_id", sa.String),
        sa.column("audience_id", sa.String),
        sa.column("language", sa.String),
        sa.column("phrase", sa.String),
        sa.column("match_type", sa.String),
        sa.column("severity", sa.String),
        sa.column("replacement_hint", sa.Text),
        sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(timezone.utc)
    op.bulk_insert(
        table,
        [
            {
                "id": phrase_id,
                "set_id": "employee_contribution_zh_v1",
                "audience_id": "employee",
                "language": "zh",
                "phrase": phrase,
                "match_type": "literal",
                "severity": "error",
                "replacement_hint": hint,
                "is_active": True,
                "created_at": now,
            }
            for phrase_id, phrase, hint in PHRASES
        ],
    )


def downgrade() -> None:
    ids = [phrase_id for phrase_id, _, _ in PHRASES]
    table = sa.table("expression_forbidden_phrases", sa.column("id", sa.String))
    op.execute(table.delete().where(table.c.id.in_(ids)))
