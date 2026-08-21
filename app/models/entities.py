from __future__ import annotations

from enum import Enum
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Enum as SqlEnum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.time import utc_now

try:
    from sqlalchemy.dialects.postgresql import JSONB
except ImportError:  # pragma: no cover - dialect import is optional in sqlite-only environments
    JSONB = None

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover - optional import for sqlite-only test environments
    Vector = None


class Base(DeclarativeBase):
    pass


class UserRole(str, Enum):
    platform_super_admin = "platform_super_admin"
    owner = "owner"
    admin = "admin"
    manager = "manager"
    employee = "employee"
    client_viewer = "client_viewer"
    super_admin = "super_admin"
    project_manager = "project_manager"
    client = "client"
    worker = "worker"


class PhotoType(str, Enum):
    project = "project"
    invoice = "invoice"


class PhotoVisibility(str, Enum):
    internal = "internal"
    client_visible = "client_visible"


class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class ProjectStatus(str, Enum):
    active = "active"
    on_hold = "on_hold"
    completed = "completed"
    archived = "archived"


class PlanCode(str, Enum):
    free = "free"
    starter = "starter"
    business = "business"
    basic = "basic"
    pro = "pro"


class SubscriptionStatus(str, Enum):
    active = "active"
    past_due = "past_due"
    canceled = "canceled"


class MembershipStatus(str, Enum):
    active = "active"
    invited = "invited"
    disabled = "disabled"


class TenantStatus(str, Enum):
    active = "active"
    suspended = "suspended"
    archived = "archived"


class CompanyApplicationStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class ReportStatus(str, Enum):
    queued = "queued"
    completed = "completed"
    failed = "failed"


class ProgressReportStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class CopilotMessageStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class TaskStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    dead_letter = "dead_letter"


class ReviewTaskType(str, Enum):
    retake_photo = "retake_photo"
    clarify_photo = "clarify_photo"
    correction_followup = "correction_followup"
    internal_note = "internal_note"


class ReviewTaskStatus(str, Enum):
    open = "open"
    acknowledged = "acknowledged"
    completed = "completed"
    cancelled = "cancelled"


class ReviewTaskEventType(str, Enum):
    created = "created"
    commented = "commented"
    acknowledged = "acknowledged"
    completed = "completed"
    cancelled = "cancelled"
    reopened = "reopened"


class ReviewSessionStatus(str, Enum):
    open = "open"
    closed = "closed"
    reopened = "reopened"


class AIAnalysisType(str, Enum):
    fast_screen = "fast_screen"
    deep_analysis = "deep_analysis"
    sticker_trigger = "sticker_trigger"
    inventory_scan = "inventory_scan"
    video_insight = "video_insight"
    lens_extraction = "lens_extraction"


class AIAnalysisStatus(str, Enum):
    active = "active"
    rejected = "rejected"


class MediaType(str, Enum):
    image = "image"
    video = "video"


class MediaAssetStatus(str, Enum):
    uploading = "uploading"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    archived = "archived"
    deleted = "deleted"


class CameraProtocol(str, Enum):
    rtsp = "rtsp"
    rtmp = "rtmp"


class CameraStatus(str, Enum):
    online = "online"
    offline = "offline"
    error = "error"


class CameraApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class AnnotationRole(str, Enum):
    employee = "employee"
    manager = "manager"


class AnnotationType(str, Enum):
    voice = "voice"
    text = "text"


class AnnotationVisibility(str, Enum):
    public = "public"
    manager_only = "manager_only"


class AnnotationStatus(str, Enum):
    processing = "processing"
    completed = "completed"
    failed = "failed"


role_enum = SqlEnum(UserRole, name="user_role", native_enum=False)
photo_type_enum = SqlEnum(PhotoType, name="photo_type", native_enum=False)
visibility_enum = SqlEnum(PhotoVisibility, name="photo_visibility", native_enum=False)
approval_status_enum = SqlEnum(ApprovalStatus, name="approval_status", native_enum=False)
project_status_enum = SqlEnum(ProjectStatus, name="project_status", native_enum=False)
plan_code_enum = SqlEnum(PlanCode, name="plan_code", native_enum=False)
subscription_status_enum = SqlEnum(SubscriptionStatus, name="subscription_status", native_enum=False)
membership_status_enum = SqlEnum(MembershipStatus, name="membership_status", native_enum=False)
tenant_status_enum = SqlEnum(TenantStatus, name="tenant_status", native_enum=False)
company_application_status_enum = SqlEnum(
    CompanyApplicationStatus,
    name="company_application_status",
    native_enum=False,
)
report_status_enum = SqlEnum(ReportStatus, name="report_status", native_enum=False)
progress_report_status_enum = SqlEnum(ProgressReportStatus, name="progress_report_status", native_enum=False)
copilot_message_status_enum = SqlEnum(CopilotMessageStatus, name="copilot_message_status", native_enum=False)
task_status_enum = SqlEnum(TaskStatus, name="task_status", native_enum=False)
review_task_type_enum = SqlEnum(ReviewTaskType, name="review_task_type", native_enum=False)
review_task_status_enum = SqlEnum(ReviewTaskStatus, name="review_task_status", native_enum=False)
review_task_event_type_enum = SqlEnum(ReviewTaskEventType, name="review_task_event_type", native_enum=False)
review_session_status_enum = SqlEnum(ReviewSessionStatus, name="review_session_status", native_enum=False)
ai_analysis_type_enum = SqlEnum(AIAnalysisType, name="ai_analysis_type", native_enum=False)
ai_analysis_status_enum = SqlEnum(AIAnalysisStatus, name="ai_analysis_status", native_enum=False)
media_type_enum = SqlEnum(MediaType, name="media_type", native_enum=False)
media_asset_status_enum = SqlEnum(MediaAssetStatus, name="media_asset_status", native_enum=False)
camera_protocol_enum = SqlEnum(CameraProtocol, name="camera_protocol", native_enum=False)
camera_status_enum = SqlEnum(CameraStatus, name="camera_status", native_enum=False)
camera_approval_status_enum = SqlEnum(
    CameraApprovalStatus,
    name="camera_approval_status",
    native_enum=False,
)
annotation_role_enum = SqlEnum(AnnotationRole, name="annotation_role", native_enum=False)
annotation_type_enum = SqlEnum(AnnotationType, name="annotation_type", native_enum=False)
annotation_visibility_enum = SqlEnum(AnnotationVisibility, name="annotation_visibility", native_enum=False)
annotation_status_enum = SqlEnum(AnnotationStatus, name="annotation_status", native_enum=False)
embedding_type = JSON()
if Vector is not None:
    embedding_type = JSON().with_variant(Vector(768), "postgresql")
analysis_result_type = JSON()
if JSONB is not None:
    analysis_result_type = JSON().with_variant(JSONB, "postgresql")


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    company_code: Mapped[str] = mapped_column(String(5), unique=True, index=True)
    company_name: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    status: Mapped[TenantStatus] = mapped_column(tenant_status_enum, default=TenantStatus.active, nullable=False)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    current_plan_id: Mapped[PlanCode | None] = mapped_column(plan_code_enum, ForeignKey("plans.plan_id"), nullable=True)
    settings_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class CompanyApplication(Base):
    __tablename__ = "company_applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    company_name: Mapped[str] = mapped_column(String(160), nullable=False)
    requested_company_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    requested_company_code: Mapped[str | None] = mapped_column(String(5), nullable=True, index=True)
    contact_name: Mapped[str] = mapped_column(String(120), nullable=False)
    contact_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    contact_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contact_title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    website_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_intro: Mapped[str] = mapped_column(Text, nullable=False)
    requested_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[CompanyApplicationStatus] = mapped_column(
        company_application_status_enum,
        default=CompanyApplicationStatus.pending,
        nullable=False,
        index=True,
    )
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    approved_company_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("companies.company_id"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    reviewed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[PlanCode] = mapped_column(plan_code_enum, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    monthly_price_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    monthly_photo_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_upload_limit: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    storage_limit_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True)
    plan_id: Mapped[PlanCode] = mapped_column(plan_code_enum, ForeignKey("plans.plan_id"), index=True)
    status: Mapped[SubscriptionStatus] = mapped_column(
        subscription_status_enum,
        default=SubscriptionStatus.active,
        nullable=False,
    )
    started_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class UsageCounter(Base):
    __tablename__ = "usage_counters"
    __table_args__ = (UniqueConstraint("company_id", "month_key", name="uq_usage_counters_company_month"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True)
    month_key: Mapped[str] = mapped_column(String(16), index=True)
    photos_this_month: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    usage_date: Mapped[str | None] = mapped_column(String(16), index=True, nullable=True)
    uploaded_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    storage_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True, index=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True, default="default")
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(role_enum, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    employee_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("employees.employee_id"), nullable=True, unique=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    last_login_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)

    memberships: Mapped[list["ProjectMember"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True, default="default")
    employee_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    api_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(64), default="worker")
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("projects.project_id"), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True, default="default")
    project_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    project_name: Mapped[str] = mapped_column(String(160))
    client_name: Mapped[str] = mapped_column(String(160))
    location: Mapped[str] = mapped_column(String(255))
    image_video_ai_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    billing_receipt_ai_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ProjectStatus] = mapped_column(project_status_enum, default=ProjectStatus.active, index=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)

    memberships: Mapped[list["ProjectMember"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class ProjectMember(Base):
    __tablename__ = "project_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.project_id"), index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    role_in_project: Mapped[str] = mapped_column(String(64), default="member")
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    project: Mapped[Project] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")


class Photo(Base):
    __tablename__ = "photos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True, default="default")
    uploaded_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    employee_id: Mapped[str] = mapped_column(String(64), ForeignKey("employees.employee_id"), index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    photo_type: Mapped[PhotoType] = mapped_column(photo_type_enum, index=True)
    file_path: Mapped[str] = mapped_column(String(1024))
    storage_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    image_url: Mapped[str] = mapped_column(String(1024))
    thumb_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    original_file_name: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    gps: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gps_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    heading: Mapped[float | None] = mapped_column(Float, nullable=True)
    pitch: Mapped[float | None] = mapped_column(Float, nullable=True)
    roll: Mapped[float | None] = mapped_column(Float, nullable=True)
    captured_at_utc: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    deleted_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    soft_deleted_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    visibility: Mapped[PhotoVisibility] = mapped_column(
        visibility_enum, default=PhotoVisibility.internal, nullable=False, index=True
    )
    approval_status: Mapped[ApprovalStatus] = mapped_column(
        approval_status_enum, default=ApprovalStatus.pending, nullable=False, index=True
    )
    approved_by_manager: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    tag_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    device_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    os_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    weather_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    ocr_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    labeling_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    defect_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scene_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duplicate_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(embedding_type, nullable=True)
    embedding_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reconstruction_batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MediaAsset(Base):
    __tablename__ = "media_assets"

    asset_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True, default="default")
    uploaded_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    media_type: Mapped[MediaType] = mapped_column(media_type_enum, index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="manual_upload")
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    original_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[MediaAssetStatus] = mapped_column(
        media_asset_status_enum,
        default=MediaAssetStatus.uploading,
        nullable=False,
        index=True,
    )
    metadata_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    completed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IPCamera(Base):
    __tablename__ = "ip_cameras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    stream_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    protocol: Mapped[CameraProtocol] = mapped_column(camera_protocol_enum, nullable=False, index=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    status: Mapped[CameraStatus] = mapped_column(
        camera_status_enum,
        default=CameraStatus.offline,
        nullable=False,
        index=True,
    )
    approval_status: Mapped[CameraApprovalStatus] = mapped_column(
        camera_approval_status_enum,
        default=CameraApprovalStatus.pending,
        nullable=False,
        index=True,
    )
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    reviewed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_check_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_alert_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class AIAnalysisLog(Base):
    __tablename__ = "ai_analysis_logs"
    __table_args__ = (
        Index("ix_ai_analysis_logs_photo_created_at", "photo_id", "created_at"),
        Index("ix_ai_analysis_logs_media_asset_created_at", "media_asset_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    photo_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("photos.id"), index=True, nullable=True)
    media_asset_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("media_assets.asset_id"), index=True, nullable=True)
    batch_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    analysis_type: Mapped[AIAnalysisType] = mapped_column(ai_analysis_type_enum, nullable=False, index=True)
    prompt_used: Mapped[str] = mapped_column(Text, nullable=False)
    model_used: Mapped[str] = mapped_column(String(255), nullable=False)
    result_data: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(analysis_result_type, nullable=True)
    status: Mapped[AIAnalysisStatus] = mapped_column(
        ai_analysis_status_enum,
        default=AIAnalysisStatus.active,
        nullable=False,
        index=True,
    )
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")


class LensDefinition(Base):
    __tablename__ = "lens_definitions"
    __table_args__ = (
        UniqueConstraint("lens_key", "scope", "company_id", "project_id", name="uq_lens_definitions_scope_key"),
        Index("ix_lens_definitions_scope_enabled", "scope", "company_id", "project_id", "is_enabled"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lens_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(24), nullable=False, default="core")
    company_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class LensDefinitionVersion(Base):
    __tablename__ = "lens_definition_versions"
    __table_args__ = (
        UniqueConstraint("lens_id", "version", name="uq_lens_definition_versions_lens_version"),
        Index("ix_lens_definition_versions_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    lens_id: Mapped[str] = mapped_column(String(64), ForeignKey("lens_definitions.id"), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    output_schema_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(analysis_result_type, nullable=True)
    validation_rules_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    model_profile_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")


class PhotoLensObservationRun(Base):
    __tablename__ = "photo_lens_observation_runs"
    __table_args__ = (
        Index("ix_photo_lens_runs_photo_lens_created", "photo_id", "lens_id", "created_at"),
        Index("ix_photo_lens_runs_model_prompt", "model_used", "prompt_hash"),
        Index("ix_photo_lens_runs_batch", "batch_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    photo_id: Mapped[int] = mapped_column(Integer, ForeignKey("photos.id"), nullable=False, index=True)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    employee_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lens_id: Mapped[str] = mapped_column(String(64), ForeignKey("lens_definitions.id"), nullable=False, index=True)
    lens_version_id: Mapped[str] = mapped_column(String(80), ForeignKey("lens_definition_versions.id"), nullable=False, index=True)
    ai_analysis_log_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("ai_analysis_logs.id"), nullable=True, index=True)
    batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    model_used: Mapped[str] = mapped_column(String(255), nullable=False)
    backend_profile_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    input_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    output_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    run_status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed", index=True)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="shadow_valid", index=True)
    result_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(analysis_result_type, nullable=True)
    raw_output_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)


class PhotoLensObservation(Base):
    __tablename__ = "photo_lens_observations"
    __table_args__ = (
        Index("ix_photo_lens_observations_scope", "company_id", "project_id", "lens_id", "validation_status"),
        Index("ix_photo_lens_observations_photo_lens", "photo_id", "lens_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    photo_id: Mapped[int] = mapped_column(Integer, ForeignKey("photos.id"), nullable=False, index=True)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    employee_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lens_id: Mapped[str] = mapped_column(String(64), ForeignKey("lens_definitions.id"), nullable=False, index=True)
    lens_version_id: Mapped[str] = mapped_column(String(80), ForeignKey("lens_definition_versions.id"), nullable=False, index=True)
    active_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("photo_lens_observation_runs.id"), nullable=False, index=True)
    observation_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(analysis_result_type, nullable=True)
    state_summary: Mapped[str] = mapped_column(String(32), nullable=False, default="mixed", index=True)
    observed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    not_observed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    uncertain_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confidence_level: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="shadow_valid", index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    supersedes_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class PhotoObservationPromotion(Base):
    __tablename__ = "photo_observation_promotions"
    __table_args__ = (
        Index("ix_photo_observation_promotions_photo_lens", "photo_id", "lens_id", "promoted_at"),
        Index("ix_photo_observation_promotions_target", "target", "gate_result"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    photo_id: Mapped[int] = mapped_column(Integer, ForeignKey("photos.id"), nullable=False, index=True)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lens_id: Mapped[str] = mapped_column(String(64), ForeignKey("lens_definitions.id"), nullable=False, index=True)
    observation_id: Mapped[str] = mapped_column(String(36), ForeignKey("photo_lens_observations.id"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("photo_lens_observation_runs.id"), nullable=False, index=True)
    promoted_fields_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(analysis_result_type, nullable=True)
    target: Mapped[str] = mapped_column(String(80), nullable=False, default="photos_tag_json")
    gate_name: Mapped[str] = mapped_column(String(120), nullable=False)
    gate_result: Mapped[str] = mapped_column(String(32), nullable=False, default="promoted_valid", index=True)
    promoted_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    promoted_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    revoked_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MediaAnnotation(Base):
    __tablename__ = "media_annotations"
    __table_args__ = (
        Index("ix_media_annotations_photo_created_at", "photo_id", "created_at"),
        Index("ix_media_annotations_media_asset_created_at", "media_asset_id", "created_at"),
        Index("ix_media_annotations_parent_created_at", "parent_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    photo_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("photos.id"), index=True, nullable=True)
    media_asset_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("media_assets.asset_id"), index=True, nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("media_annotations.id"), index=True, nullable=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    role_at_time: Mapped[AnnotationRole] = mapped_column(annotation_role_enum, nullable=False, index=True)
    annotation_type: Mapped[AnnotationType] = mapped_column(annotation_type_enum, nullable=False, index=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    translations_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    audio_file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    visibility: Mapped[AnnotationVisibility] = mapped_column(
        annotation_visibility_enum,
        default=AnnotationVisibility.public,
        nullable=False,
        index=True,
    )
    status: Mapped[AnnotationStatus] = mapped_column(
        annotation_status_enum,
        default=AnnotationStatus.completed,
        nullable=False,
        index=True,
    )
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class GeneratedReport(Base):
    __tablename__ = "generated_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    status: Mapped[ReportStatus] = mapped_column(report_status_enum, default=ReportStatus.queued, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255))
    prompt: Mapped[str] = mapped_column(Text)
    source_photo_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    markdown_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    completed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProgressReport(Base):
    __tablename__ = "progress_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.project_id"), index=True)
    status: Mapped[ProgressReportStatus] = mapped_column(
        progress_report_status_enum,
        default=ProgressReportStatus.pending,
        nullable=False,
        index=True,
    )
    source_photo_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    report_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    completed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvidenceObservation(Base):
    __tablename__ = "evidence_observations"
    __table_args__ = (
        Index("ix_evidence_observations_company_project_type", "company_id", "project_id", "observation_type"),
        Index("ix_evidence_observations_photo_type", "photo_id", "observation_type"),
        Index("ix_evidence_observations_project_observed_at", "project_id", "observed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("projects.project_id"), index=True, nullable=True)
    photo_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("photos.id"), index=True, nullable=True)
    media_asset_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("media_assets.asset_id"), index=True, nullable=True)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    observation_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    normalized_key: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ReceiptFact(Base):
    __tablename__ = "receipt_facts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("projects.project_id"), index=True, nullable=True)
    photo_id: Mapped[int] = mapped_column(Integer, ForeignKey("photos.id"), unique=True, index=True)
    vendor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    gallons: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fuel_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    purchaser_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    employee_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("employees.employee_id"), nullable=True, index=True)
    receipt_timestamp: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    currency_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    has_pump_photo: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    facts_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ExpressionAudience(Base):
    __tablename__ = "expression_audiences"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_language: Mapped[str] = mapped_column(String(16), default="zh", nullable=False)
    visibility_policy_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    forbidden_phrase_set_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ExpressionPromptTemplate(Base):
    __tablename__ = "expression_prompt_templates"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="expression", index=True)
    default_audience_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("expression_audiences.id"),
        nullable=True,
        index=True,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ExpressionPromptVersion(Base):
    __tablename__ = "expression_prompt_versions"
    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_expression_prompt_versions_template_version"),
        Index("ix_expression_prompt_versions_template_status", "template_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    template_id: Mapped[str] = mapped_column(String(96), ForeignKey("expression_prompt_templates.id"), index=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    user_prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    few_shot_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    json_schema_override: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False, index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ExpressionPromptBinding(Base):
    __tablename__ = "expression_prompt_bindings"
    __table_args__ = (
        Index("ix_expression_prompt_bindings_scope", "scope_type", "scope_id"),
        Index("ix_expression_prompt_bindings_template_priority", "template_id", "priority"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scope_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    template_id: Mapped[str] = mapped_column(String(96), ForeignKey("expression_prompt_templates.id"), index=True)
    prompt_version_id: Mapped[str] = mapped_column(String(128), ForeignKey("expression_prompt_versions.id"), index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False, index=True)
    effective_from: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_until: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ExpressionOutputContract(Base):
    __tablename__ = "expression_output_contracts"
    __table_args__ = (
        UniqueConstraint(
            "artifact_type",
            "audience_id",
            "version",
            name="uq_expression_output_contracts_artifact_audience_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    artifact_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    audience_id: Mapped[str] = mapped_column(String(64), ForeignKey("expression_audiences.id"), index=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    json_schema: Mapped[dict[str, Any] | list[Any]] = mapped_column(JSON, nullable=False)
    required_fact_paths_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    forbidden_claims_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ExpressionForbiddenPhrase(Base):
    __tablename__ = "expression_forbidden_phrases"
    __table_args__ = (
        Index("ix_expression_forbidden_phrases_set_language", "set_id", "language"),
        Index("ix_expression_forbidden_phrases_audience_language", "audience_id", "language"),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    set_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    audience_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("expression_audiences.id"),
        nullable=True,
        index=True,
    )
    language: Mapped[str] = mapped_column(String(16), default="zh", nullable=False, index=True)
    phrase: Mapped[str] = mapped_column(String(255), nullable=False)
    match_type: Mapped[str] = mapped_column(String(32), default="literal", nullable=False)
    severity: Mapped[str] = mapped_column(String(32), default="error", nullable=False, index=True)
    replacement_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class FactSnapshot(Base):
    __tablename__ = "fact_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "scope_type",
            "scope_key_hash",
            "assembler_version",
            name="uq_fact_snapshots_company_scope_assembler",
        ),
        Index("ix_fact_snapshots_company_scope", "company_id", "scope_type"),
        Index("ix_fact_snapshots_employee_project", "employee_id", "project_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True)
    employee_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("employees.employee_id"), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("projects.project_id"), nullable=True, index=True)
    scope_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope_key_json: Mapped[dict[str, Any] | list[Any]] = mapped_column(JSON, nullable=False)
    scope_key_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    assembler_version: Mapped[str] = mapped_column(String(32), nullable=False)
    source_manifest_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    facts_json: Mapped[dict[str, Any] | list[Any]] = mapped_column(JSON, nullable=False)
    fact_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    coverage_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)


class ExpressionArtifact(Base):
    __tablename__ = "expression_artifacts"
    __table_args__ = (
        Index("ix_expression_artifacts_company_scope", "company_id", "scope_type"),
        Index("ix_expression_artifacts_scope_audience", "scope_type", "audience_id", "artifact_type"),
        Index("ix_expression_artifacts_promoted_created", "promoted", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_job_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("task_jobs.id"), nullable=True, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True)
    fact_snapshot_id: Mapped[str] = mapped_column(String(36), ForeignKey("fact_snapshots.id"), index=True)
    prompt_version_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("expression_prompt_versions.id"),
        nullable=True,
        index=True,
    )
    contract_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("expression_output_contracts.id"),
        nullable=True,
        index=True,
    )
    scope_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope_key_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    artifact_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    audience_id: Mapped[str] = mapped_column(String(64), ForeignKey("expression_audiences.id"), index=True)
    language: Mapped[str] = mapped_column(String(16), default="zh", nullable=False, index=True)
    structured_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    raw_model_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    rendered_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    validation_errors_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(255), nullable=True)
    backend_profile_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    promoted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    supersedes_artifact_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("expression_artifacts.id"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ExpressionEvalRun(Base):
    __tablename__ = "expression_eval_runs"
    __table_args__ = (
        Index("ix_expression_eval_runs_type_status", "run_type", "status"),
        Index("ix_expression_eval_runs_artifact_audience", "artifact_type", "audience_id"),
        Index("ix_expression_eval_runs_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    company_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("companies.company_id"), nullable=True, index=True)
    run_type: Mapped[str] = mapped_column(String(32), default="golden_replay", nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="created", nullable=False, index=True)
    trigger_source: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    artifact_type: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    audience_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("expression_audiences.id"), nullable=True, index=True)
    prompt_version_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("expression_prompt_versions.id"),
        nullable=True,
        index=True,
    )
    contract_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("expression_output_contracts.id"),
        nullable=True,
        index=True,
    )
    runner_version: Mapped[str] = mapped_column(String(32), default="eval_run_v1", nullable=False)
    input_manifest_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    summary_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ExpressionEvalItem(Base):
    __tablename__ = "expression_eval_items"
    __table_args__ = (
        Index("ix_expression_eval_items_run_status", "eval_run_id", "status"),
        Index("ix_expression_eval_items_case", "case_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    eval_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("expression_eval_runs.id"), index=True)
    artifact_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("expression_artifacts.id"), nullable=True, index=True)
    fact_snapshot_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("fact_snapshots.id"), nullable=True, index=True)
    case_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    validator_name: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    expected_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    actual_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    validation_errors_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    metrics_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)


class CopilotConversation(Base):
    __tablename__ = "copilot_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("projects.project_id"), index=True, nullable=True)
    created_by_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    preferred_backend_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preferred_mode: Mapped[str] = mapped_column(String(32), default="standard", nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class CopilotMessage(Base):
    __tablename__ = "copilot_messages"
    __table_args__ = (
        Index("ix_copilot_messages_conversation_created_at", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), ForeignKey("copilot_conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[CopilotMessageStatus] = mapped_column(
        copilot_message_status_enum,
        default=CopilotMessageStatus.completed,
        nullable=False,
        index=True,
    )
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    selected_backend_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    selected_backend_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    selected_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    context_summary_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    completed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CopilotMessageSource(Base):
    __tablename__ = "copilot_message_sources"
    __table_args__ = (
        Index("ix_copilot_message_sources_message_type", "message_id", "source_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(36), ForeignKey("copilot_messages.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class TaskJob(Base):
    __tablename__ = "task_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    task_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[TaskStatus] = mapped_column(task_status_enum, default=TaskStatus.queued, index=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    available_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, index=True, nullable=False)
    started_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    related_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    related_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class PhotoComment(Base):
    __tablename__ = "photo_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    photo_id: Mapped[int] = mapped_column(Integer, ForeignKey("photos.id"), index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    comment: Mapped[str] = mapped_column(Text)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ReviewTask(Base):
    __tablename__ = "review_tasks"
    __table_args__ = (
        Index("ix_review_tasks_project_status", "company_id", "project_id", "status"),
        Index("ix_review_tasks_employee_status", "company_id", "assigned_employee_id", "status"),
        Index("ix_review_tasks_related_photo", "related_photo_id"),
        Index("ix_review_tasks_completion_photo", "completion_photo_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    related_photo_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("photos.id"), nullable=True, index=True)
    task_type: Mapped[ReviewTaskType] = mapped_column(review_task_type_enum, nullable=False, index=True)
    status: Mapped[ReviewTaskStatus] = mapped_column(
        review_task_status_enum,
        default=ReviewTaskStatus.open,
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    assigned_employee_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    message: Mapped[str] = mapped_column(Text)
    due_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completion_photo_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("photos.id"), nullable=True, index=True)
    acknowledged_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by_employee_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    completed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by_employee_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    cancelled_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    metadata_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ReviewTaskEvent(Base):
    __tablename__ = "review_task_events"
    __table_args__ = (
        Index("ix_review_task_events_task_created", "task_public_id", "created_at"),
        Index("ix_review_task_events_project_created", "company_id", "project_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    task_public_id: Mapped[str] = mapped_column(String(36), ForeignKey("review_tasks.public_id"), index=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[ReviewTaskEventType] = mapped_column(review_task_event_type_enum, nullable=False, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    actor_employee_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)


class ReviewSession(Base):
    __tablename__ = "review_sessions"
    __table_args__ = (
        Index("ix_review_sessions_project_date", "company_id", "project_id", "review_date"),
        Index("ix_review_sessions_project_status", "company_id", "project_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    company_id: Mapped[str] = mapped_column(String(64), ForeignKey("companies.company_id"), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    review_date: Mapped[str] = mapped_column(String(10), index=True)
    status: Mapped[ReviewSessionStatus] = mapped_column(
        review_session_status_enum,
        default=ReviewSessionStatus.open,
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    closed_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    summary_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    closed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    expires_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_memberships_tenant_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[MembershipStatus] = mapped_column(
        membership_status_enum,
        default=MembershipStatus.active,
        nullable=False,
    )
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    expires_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    company_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("companies.company_id"), nullable=True, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    target_type: Mapped[str] = mapped_column(String(120), index=True)
    target_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    detail_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class GpuMetricSample(Base):
    __tablename__ = "gpu_metric_samples"
    __table_args__ = (
        Index("ix_gpu_metric_samples_host_gpu_time", "hostname", "gpu_index", "sampled_at"),
        Index("ix_gpu_metric_samples_sampled_at", "sampled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sampled_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    hostname: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    gpu_index: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    gpu_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    utilization_gpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    utilization_memory_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_total_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_used_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_free_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_draw_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_limit_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    fan_speed_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class GpuMetricHourlyRollup(Base):
    __tablename__ = "gpu_metric_hourly_rollups"
    __table_args__ = (
        UniqueConstraint("hostname", "gpu_index", "bucket_start", name="uq_gpu_metric_hourly_host_gpu_bucket"),
        Index("ix_gpu_metric_hourly_bucket_start", "bucket_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bucket_start: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
    hostname: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    gpu_index: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    gpu_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_gpu_utilization_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_gpu_utilization_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_memory_utilization_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_memory_utilization_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_memory_used_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_memory_used_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_power_draw_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_power_draw_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    saturated_sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    memory_pressure_sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
