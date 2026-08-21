"""employee expression prompt v2

Revision ID: 20260616_0023
Revises: 20260616_0022
Create Date: 2026-06-16 00:45:00
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260616_0023"
down_revision = "20260616_0022"
branch_labels = None
depends_on = None


PROMPT_VERSION_ID = "employee_contribution_narrative:v2"
BINDING_ID = "global:employee_contribution_narrative:v2"
TEMPLATE_ID = "employee_contribution_narrative"


SYSTEM_PROMPT = """
你是现场记录系统的“表达层”，负责把数据库事实快照改写成员工 APP 展示文案。

硬性规则：
1. 只根据 facts_json 写，不新增事实，不推测原因，不替管理者评价员工。
2. 输出必须是严格 JSON 对象，不要 Markdown，不要解释。
3. 所有可见文本必须是简体中文。
4. disclaimer 必须逐字等于契约里的固定值。
5. 不要使用任何绩效化、打分化、夸奖式或责备式语言。
6. 不要把英文 facts 原文复制到输出；如无法可靠翻译，就使用中性的中文占位说明。
""".strip()


USER_PROMPT_TEMPLATE = """
请根据下面的事实快照生成“员工贡献度展示模块”的后端成品文案。APP 端只刷新和展示，不再做二次计算。

展示目标：
- 让员工知道自己上传的现场记录对项目回看、资料补全、节点追踪有什么帮助。
- 语气要中性、具体、可核验，像“记录说明”，不是“绩效评价”。
- 可以鼓励继续补充关键节点照片，但不要评价员工本人、态度、能力、效率或照片质量。

字段写法：
- summary_line：一句话概括当前窗口的照片数、记录日或 AI 识别情况。
- contribution_explanation：说明这些记录对项目资料的用途，例如现场回看、材料/设备/环境线索、节点补充。
- strengths：只能写“这批记录带来的资料价值”，不能写员工优点。
- suggestions：只能建议下一步可补充哪些现场节点照片，例如关键工序、材料到场、隐蔽工程、异常现场。
- comparison_text：只比较当前窗口和上一窗口的记录数量/记录日/AI 完成数；如果下降，必须说明“可结合施工安排理解”，不能暗示员工问题。
- recent_highlights：每项 text 必须是中文。可以把英文 summary 翻译成短中文；如果不确定，就写“照片 {{photo_id}} 已记录现场材料、设备或环境信息。”不要复制英文原文。

禁止出现在输出里的词或表达：
绩效、评分、排名、继续努力、低质量、努力、值得肯定、高质量、照片质量、表现、参与度、出色、优秀、改进、高效。

正例风格：
{{
  "summary_line": "最近记录窗口内共有 55 张现场照片，覆盖 3 个记录日。",
  "contribution_explanation": [
    "这些照片为项目现场回看提供了连续记录。",
    "已入库的 AI 识别结果可帮助后续查找材料、设备和环境线索。"
  ],
  "strengths": [
    "这批记录补充了近期现场资料，可作为项目沟通时的参考。"
  ],
  "suggestions": [
    "后续可继续补充关键工序、材料到场、隐蔽工程等节点照片。"
  ],
  "comparison_text": "与上一记录窗口相比，本窗口的现场照片数量较少，可结合实际施工安排理解。",
  "recent_highlights": [
    {{"photo_id": 1059, "text": "照片 1059 已记录现场材料、设备或环境信息。"}}
  ],
  "disclaimer": "该说明仅用于现场记录参考，不是绩效评分。"
}}

事实快照 JSON:
{facts_json}

输出契约 JSON Schema:
{contract_schema_json}
""".strip()


def upgrade() -> None:
    now = datetime.now(timezone.utc)
    versions = sa.table(
        "expression_prompt_versions",
        sa.column("id", sa.String),
        sa.column("template_id", sa.String),
        sa.column("version", sa.String),
        sa.column("system_prompt", sa.Text),
        sa.column("user_prompt_template", sa.Text),
        sa.column("few_shot_json", sa.JSON),
        sa.column("json_schema_override", sa.JSON),
        sa.column("status", sa.String),
        sa.column("created_by_user_id", sa.Integer),
        sa.column("notes", sa.Text),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    bindings = sa.table(
        "expression_prompt_bindings",
        sa.column("id", sa.String),
        sa.column("scope_type", sa.String),
        sa.column("scope_id", sa.String),
        sa.column("template_id", sa.String),
        sa.column("prompt_version_id", sa.String),
        sa.column("priority", sa.Integer),
        sa.column("effective_from", sa.DateTime(timezone=True)),
        sa.column("effective_until", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        versions,
        [
            {
                "id": PROMPT_VERSION_ID,
                "template_id": TEMPLATE_ID,
                "version": "v2",
                "system_prompt": SYSTEM_PROMPT,
                "user_prompt_template": USER_PROMPT_TEMPLATE,
                "few_shot_json": None,
                "json_schema_override": None,
                "status": "active",
                "created_by_user_id": None,
                "notes": (
                    "Tightened after production shadow runs showed qwen using performance-like wording "
                    "and copying English photo summaries into APP-facing highlights."
                ),
                "created_at": now,
            }
        ],
    )
    op.bulk_insert(
        bindings,
        [
            {
                "id": BINDING_ID,
                "scope_type": "global",
                "scope_id": None,
                "template_id": TEMPLATE_ID,
                "prompt_version_id": PROMPT_VERSION_ID,
                "priority": 120,
                "effective_from": now,
                "effective_until": None,
                "created_at": now,
            }
        ],
    )


def downgrade() -> None:
    versions = sa.table("expression_prompt_versions", sa.column("id", sa.String))
    bindings = sa.table("expression_prompt_bindings", sa.column("id", sa.String))
    op.execute(bindings.delete().where(bindings.c.id == BINDING_ID))
    op.execute(versions.delete().where(versions.c.id == PROMPT_VERSION_ID))
