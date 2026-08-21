"""progress report multi image prompt registry shadow

Revision ID: 20260616_0040
Revises: 20260616_0039
Create Date: 2026-06-16 19:05:00
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260616_0040"
down_revision = "20260616_0039"
branch_labels = None
depends_on = None


TEMPLATE_ID = "progress_report_multi_image"
PROMPT_VERSION_ID = "progress_report_multi_image:v1"
BINDING_ID = "global:progress_report_multi_image:v1"


USER_PROMPT_TEMPLATE = """
你是一个资深的工程监理，正在为项目经理生成正式的进度演变报告。
本项目的最高审查标准是：{project_prompt_text}
{custom_prompt_instruction}
以下是同一施工位置按时间顺序拍摄的多张现场照片。请结合拍摄角度差异、拍摄距离变化和现场遮挡，尽量消除视觉误差。
请只基于这些原始图片和元数据进行判断，不要引用图片外的信息，不要臆测看不见的施工内容。
报告目标不是简单描述图片，而是帮助项目经理判断：进度是否推进、风险是否变大、材料/工具/库存是否支持下一步施工、现场是否有积水或整理问题、哪些事项需要马上安排人员跟进。
每条重要判断尽量写清楚依据来自哪些 photo_id；如果只能从单张图推断，必须说明证据范围有限。
如果下面附带了既有的单图 AI 识别结果，请把它们当作弱约束参考：当前图像与既有结论一致时，优先沿用已有的对象命名。
如果跨图片的一致性提示中已经反复把同一对象命名为某个设备，除非当前图片有明显相反证据，否则不要重新换成别的具体机器名称。
如果你无法高把握确认某个设备或物体的具体类型，请使用保守描述，例如“黑色矩形设备”或“地面设备”，不要仅凭猜测就写成咖啡机、碎纸机或取暖器。
你必须输出一个严格合法的 JSON 对象，不要输出 Markdown，不要包裹 ```json 代码块，不要输出任何 JSON 之外的解释文字。
JSON 字段要求：
- overall_progress_percent 必须是 0 到 100 的整数。
- overall_status 只能是 "on_track"、"at_risk"、"blocked"、"unknown" 之一。
- confidence_level 只能是 "high"、"medium"、"low" 之一。
- 所有列表字段都必须返回数组，没有内容时返回空数组。
- timeline_observations 必须按时间顺序返回，并尽量覆盖每一张图片。
- timeline_observations[].photo_id 必须引用下面提供的真实 photo_id。
- manager_brief 要让项目经理一眼看懂是否需要立即介入。
- material_inventory_signals 要区分“可见库存线索”和“无法确认数量”，不要编造精确数量。
- water_housekeeping_signals 要专门记录积水、泥泞、垃圾、通道遮挡和场地整理情况。
- uncertain_items 用于承认无法可靠识别的设备/材料，并说明为什么不确定。
- immediate_decisions 必须是经理可以立刻执行或安排的事项，不要写空泛建议。
- evidence_limitations 必须明确影响结论可靠性的限制。
输出 JSON 结构示例：
{example_schema_json}

{existing_ai_contexts_section}{preferred_ai_hints_section}照片元数据：
{photo_metadata_lines}
""".strip()


def upgrade() -> None:
    now = datetime.now(timezone.utc)
    templates = sa.table(
        "expression_prompt_templates",
        sa.column("id", sa.String),
        sa.column("slug", sa.String),
        sa.column("title", sa.String),
        sa.column("artifact_type", sa.String),
        sa.column("stage", sa.String),
        sa.column("default_audience_id", sa.String),
        sa.column("description", sa.Text),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
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
        templates,
        [
            {
                "id": TEMPLATE_ID,
                "slug": TEMPLATE_ID,
                "title": "Progress report multi-image prompt",
                "artifact_type": "progress_report_generation",
                "stage": "vision_report",
                "default_audience_id": "project_manager",
                "description": "Shadow registry copy of the legacy multi-image progress report generation prompt.",
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    op.bulk_insert(
        versions,
        [
            {
                "id": PROMPT_VERSION_ID,
                "template_id": TEMPLATE_ID,
                "version": "v1",
                "system_prompt": "Legacy multi-image progress report prompt registry shadow.",
                "user_prompt_template": USER_PROMPT_TEMPLATE,
                "few_shot_json": None,
                "json_schema_override": None,
                "status": "active",
                "created_by_user_id": None,
                "notes": (
                    "Shadow-only parity seed. Runtime progress reports still use "
                    "app.services.reports.build_multi_image_progress_prompt."
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
                "priority": 100,
                "effective_from": now,
                "effective_until": None,
                "created_at": now,
            }
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM expression_prompt_bindings WHERE id = 'global:progress_report_multi_image:v1'")
    op.execute("DELETE FROM expression_prompt_versions WHERE id = 'progress_report_multi_image:v1'")
    op.execute("DELETE FROM expression_prompt_templates WHERE id = 'progress_report_multi_image'")
