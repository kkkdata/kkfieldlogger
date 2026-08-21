"""project manager decision brief forbidden phrases

Revision ID: 20260616_0035
Revises: 20260616_0034
Create Date: 2026-06-16 09:05:00
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260616_0035"
down_revision = "20260616_0034"
branch_labels = None
depends_on = None


PHRASES = [
    ("manager_overview_v1:should", "en", r"(?i)\bshould\b", "regex", "Use observed status wording instead of advice."),
    ("manager_overview_v1:must", "en", r"(?i)\bmust\b", "regex", "Use observed status wording instead of commands."),
    ("manager_overview_v1:ensure", "en", r"(?i)\bensure\b", "regex", "Avoid action directives in shadow briefs."),
    ("manager_overview_v1:priority", "en", r"(?i)\bpriority\b", "regex", "Use rule status only; do not assign priority."),
    ("manager_overview_v1:recommend", "en", r"(?i)\brecommend(?:ed|ation)?\b", "regex", "Avoid recommendations in decision briefs."),
    ("manager_overview_v1:urgent", "en", r"(?i)\burgent\b", "regex", "Use deterministic severity only."),
    ("manager_overview_v1:rank", "en", r"(?i)\brank(?:ing)?\b", "regex", "Do not rank people, teams, or projects."),
    ("manager_overview_v1:performance_score", "en", r"(?i)\bperformance score\b", "regex", "Do not create performance scoring."),
    ("manager_overview_zh_v1:responsibility", "zh", "责任归因", "literal", "只说明数据库事实和规则状态，不做责任判断。"),
    ("manager_overview_zh_v1:employee_rank", "zh", "员工排名", "literal", "不对员工做排名。"),
    ("manager_overview_zh_v1:performance_score", "zh", "绩效评分", "literal", "不输出绩效评分。"),
    ("manager_overview_zh_v1:performance", "zh", "绩效", "literal", "改为记录覆盖、报告状态或表达产物状态。"),
    ("manager_overview_zh_v1:must", "zh", "必须", "literal", "避免指令式表达。"),
    ("manager_overview_zh_v1:should", "zh", "应该", "literal", "避免建议式表达。"),
    ("manager_overview_zh_v1:urgent", "zh", "紧急", "literal", "使用规则状态，不制造紧急感。"),
    ("manager_overview_zh_v1:priority", "zh", "优先级", "literal", "不生成优先级。"),
    ("manager_overview_zh_v1:recommend", "zh", "建议", "literal", "避免行动建议。"),
]


def _bulk_insert_ignore(table: sa.TableClause, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        op.execute(insert(table).values(rows).on_conflict_do_nothing(index_elements=["id"]))
        return
    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert

        op.execute(insert(table).values(rows).prefix_with("OR IGNORE"))
        return
    for row in rows:
        row_id = row.get("id")
        exists = bind.execute(sa.select(sa.literal(1)).select_from(table).where(table.c.id == row_id)).first()
        if exists is None:
            op.bulk_insert(table, [row])


def upgrade() -> None:
    now = datetime.now(timezone.utc)
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
    _bulk_insert_ignore(
        table,
        [
            {
                "id": phrase_id,
                "set_id": "manager_overview_zh_v1",
                "audience_id": "project_manager",
                "language": language,
                "phrase": phrase,
                "match_type": match_type,
                "severity": "error",
                "replacement_hint": hint,
                "is_active": True,
                "created_at": now,
            }
            for phrase_id, language, phrase, match_type, hint in PHRASES
        ],
    )


def downgrade() -> None:
    table = sa.table("expression_forbidden_phrases", sa.column("id", sa.String))
    op.execute(table.delete().where(table.c.id.in_([phrase_id for phrase_id, *_ in PHRASES])))
