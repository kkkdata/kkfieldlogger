from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="KK_", extra="ignore")

    app_name: str = "KK Field Logger"
    app_version: str = "dev"
    portal_title: str = "KK Field Logger Portal"
    company_name: str = "K&K Data Service Inc."
    # "saas" (default, multi-tenant cloud) or "private" (NAS/appliance:
    # single company, platform surfaces hidden, no public registration,
    # quotas disabled). See docs/nas/dual-track-plan.md.
    deployment_profile: str = "saas"
    # When set and KK_SESSION_SECRET is absent/default, the secret is read
    # from this file, generated on first boot (NAS profile mounts it on a
    # persistent volume).
    session_secret_file: str | None = None
    database_url: str = Field(
        default="sqlite:///./kk_field_logger.db",
        validation_alias=AliasChoices("database_url", "KK_DATABASE_URL", "DATABASE_URL"),
    )
    # When set and the file exists, its contents replace the password in
    # database_url. NAS installs generate a random per-install password on
    # the shared config volume (see deploy/nas/docker-compose.yml) instead
    # of shipping a fixed credential.
    database_password_file: str | None = None
    media_root: str = "/mnt/movies/kkdata"
    media_url_prefix: str = "/media"
    public_base_url: str = "http://localhost"
    cors_allow_origins: str = (
        "http://localhost,"
        "http://127.0.0.1,"
        "http://localhost:3000,"
        "http://127.0.0.1:3000,"
        "http://localhost:4173,"
        "http://127.0.0.1:4173,"
        "http://localhost:5173,"
        "http://127.0.0.1:5173,"
        "http://localhost:8000,"
        "http://127.0.0.1:8000,"
        "capacitor://localhost,"
        "ionic://localhost,"
        "null"
    )
    cors_allow_origin_regex: str | None = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
    session_secret: str = Field(
        default="change-me",
        validation_alias=AliasChoices("session_secret", "KK_SESSION_SECRET", "SESSION_SECRET"),
    )
    default_timezone: str = "America/Chicago"
    upload_max_mb: int = 50
    default_visibility: str = "internal"
    default_admin_username: str = "admin"
    default_admin_password: str = "ChangeMe123!"
    default_admin_email: str = "admin@example.com"
    default_admin_display_name: str = "System Administrator"
    default_company_id: str = "default"
    default_company_name: str = "Default Company"
    email_backend: str = "smtp"
    email_sender_name: str = "KK Field Logger"
    email_sender_address: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_starttls: bool = True
    smtp_use_ssl: bool = False
    sendgrid_api_key: str | None = None
    sendgrid_from_email: str | None = None
    password_reset_hours: int = 2
    support_email: str = "support@example.com"
    site_inquiry_email: str = "inquiry@kkdatasvc.com"
    site_inquiry_copy_email: str | None = "ray@kkdatasvc.com"
    site_inquiry_min_submit_seconds: int = 3
    site_inquiry_rate_limit_per_minute: int = 6
    register_rate_limit_per_minute: int = 3
    queue_dead_letter_alert_threshold_24h: int = 5
    queue_oldest_job_age_warning_minutes: int = 30
    # Retention/compaction (0 disables a rule). Runs daily in the worker.
    retention_superseded_ai_log_days: int = 180
    retention_audit_log_days: int = 365
    retention_recycle_bin_days: int = 0
    retention_completed_job_days: int = 30
    environment: str = "development"
    log_level: str = "INFO"
    log_dir: str = "logs"
    log_retention_days: int = 7
    system_metrics_interval_seconds: int = 60
    otel_enabled: bool = False
    otel_endpoint: str | None = None
    queue_worker_poll_seconds: int = 5
    queue_worker_batch_size: int = 10
    queue_inline_request_worker_enabled: bool = True
    queue_max_attempts: int = 3
    queue_retry_delay_seconds: int = 30
    queue_backlog_warning_threshold: int = 360
    queue_max_pending_jobs_per_tenant: int = 4320
    queue_running_timeout_seconds: int = 3600
    queue_stale_recovery_batch_size: int = 25
    ai_embedding_max_text_chars: int = 4000
    ai_rate_limit_per_minute: int = 300
    ai_healthcheck_interval_seconds: int = 300
    ai_healthcheck_timeout_seconds: int = 10
    ai_backend_failure_cooldown_seconds: int = 120
    ai_min_auditable_image_bytes: int = 512
    portal_photos_cache_ttl_seconds: int = 30
    semantic_search_cache_ttl_seconds: int = 30
    high_frequency_rate_limit_per_minute: int = 120
    portal_media_rate_limit_per_minute: int = 180
    media_signed_url_ttl_seconds: int = 604800
    webhook_rate_limit_per_minute: int = 240
    mobile_diagnostic_log_rate_limit_per_hour: int = 6
    camera_webhook_ip_allowlist: str = "127.0.0.1,::1,localhost,testclient"
    camera_rtsp_poll_interval_seconds: int = 900
    camera_capture_clip_seconds: int = 15
    camera_capture_timeout_seconds: int = 45
    camera_webhook_token: str = "change-me-camera-webhook"
    media_cleanup_interval_seconds: int = 86400
    system_health_alert_cooldown_seconds: int = 1800
    gpu_metrics_enabled: bool = True
    gpu_metrics_sample_interval_seconds: int = 1
    gpu_metrics_flush_interval_seconds: int = 30
    gpu_metrics_query_timeout_seconds: int = 2
    gpu_metrics_raw_retention_hours: int = 72
    gpu_metrics_rollup_retention_days: int = 180
    gpu_metrics_saturation_threshold_percent: float = 85.0
    gpu_metrics_memory_pressure_threshold_percent: float = 85.0
    gpu_metrics_electricity_rate_per_kwh: float = 0.16
    report_max_selected_photos: int = 100
    progress_report_max_selected_photos: int = 12
    report_max_markdown_chars: int = 120000
    expression_text_backend_url: str | None = None
    expression_text_backend_model: str | None = None
    lens_vision_backend_url: str | None = None
    lens_vision_backend_model: str | None = None
    lens_backfill_sleep_seconds: float = 0.0

    @property
    def is_private_deployment(self) -> bool:
        return self.deployment_profile.strip().lower() == "private"

    def model_post_init(self, __context) -> None:
        # NAS installs keep the database password in a file generated at
        # install time; splice it into the URL so no credential is fixed in
        # the compose file or container environment.
        if self.database_password_file:
            pw_path = Path(self.database_password_file)
            try:
                if pw_path.exists():
                    password = pw_path.read_text(encoding="utf-8").strip()
                    if password:
                        from urllib.parse import quote as _quote

                        parsed = urlsplit(self.database_url)
                        if parsed.hostname:
                            auth = parsed.username or ""
                            netloc = f"{auth}:{_quote(password, safe='')}@{parsed.hostname}"
                            if parsed.port:
                                netloc += f":{parsed.port}"
                            self.database_url = urlunsplit(parsed._replace(netloc=netloc))
            except OSError:
                pass

        # Private/NAS deployments have no operator to set a strong secret;
        # generate one on first boot and persist it on the config volume.
        if self.session_secret in {"", "change-me"} and self.session_secret_file:
            secret_path = Path(self.session_secret_file)
            try:
                if secret_path.exists():
                    stored = secret_path.read_text(encoding="utf-8").strip()
                    if stored:
                        self.session_secret = stored
                        return
                import secrets as _secrets

                generated = _secrets.token_urlsafe(48)
                secret_path.parent.mkdir(parents=True, exist_ok=True)
                secret_path.write_text(generated, encoding="utf-8")
                try:
                    secret_path.chmod(0o600)
                except OSError:
                    pass
                self.session_secret = generated
            except OSError:
                # Unwritable volume: keep the configured value; the app still
                # boots and the operator can fix the mount.
                pass

    @property
    def media_root_path(self) -> Path:
        return Path(self.media_root)

    @property
    def photos_root(self) -> Path:
        return self.media_root_path / "photos"

    @property
    def reports_root(self) -> Path:
        return self.media_root_path / "reports"

    @property
    def media_assets_root(self) -> Path:
        return self.media_root_path / "media_assets"

    @property
    def media_upload_chunks_root(self) -> Path:
        return self.media_assets_root / "_chunks"

    @property
    def media_frames_root(self) -> Path:
        return self.media_assets_root / "_frames"

    @property
    def media_cold_storage_root(self) -> Path:
        return self.media_assets_root / "_cold_storage"

    @property
    def camera_clips_root(self) -> Path:
        return self.media_assets_root / "_camera_clips"

    @property
    def logs_root(self) -> Path:
        return Path(self.log_dir)

    @property
    def logo_path(self) -> str:
        return "/static/img/kk-logo.png"

    @property
    def cors_allow_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_allow_origins.split(",") if item.strip()]


def load_settings() -> Settings:
    return Settings()
