from __future__ import annotations

import os
import time
from pathlib import Path

from app.core.config import load_settings
from app.core.observability import get_logger, initialize_observability
from app.db.session import create_engine_from_settings, create_session_maker
from app.services.ai_pipeline import perform_ai_healthcheck
from app.services.bootstrap import bootstrap_platform_data
from app.services.camera_scheduler import poll_enabled_rtsp_cameras
from app.services.job_queue import process_due_jobs
from app.services.media_pipeline import cleanup_expired_media_assets
from app.services.retention import run_retention_pass
from app.services.settings import bootstrap_system_settings
from app.services.system_health import perform_system_health_audit


def main() -> None:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    settings.photos_root.mkdir(parents=True, exist_ok=True)
    settings.reports_root.mkdir(parents=True, exist_ok=True)
    settings.media_assets_root.mkdir(parents=True, exist_ok=True)
    settings.media_upload_chunks_root.mkdir(parents=True, exist_ok=True)
    settings.media_frames_root.mkdir(parents=True, exist_ok=True)
    settings.media_cold_storage_root.mkdir(parents=True, exist_ok=True)
    settings.camera_clips_root.mkdir(parents=True, exist_ok=True)
    settings.logs_root.mkdir(parents=True, exist_ok=True)
    observability = initialize_observability(settings, component="worker", session_maker=session_maker)
    logger = get_logger("kkfieldlogger.worker")

    with session_maker() as db:
        bootstrap_platform_data(db, settings)
        bootstrap_system_settings(db, settings)

    logger.info(
        "worker_started",
        queue_worker_poll_seconds=settings.queue_worker_poll_seconds,
        queue_worker_batch_size=settings.queue_worker_batch_size,
        queue_max_attempts=settings.queue_max_attempts,
        queue_running_timeout_seconds=settings.queue_running_timeout_seconds,
        queue_stale_recovery_batch_size=settings.queue_stale_recovery_batch_size,
    )
    next_ai_healthcheck_at = 0.0
    next_camera_poll_at = 0.0
    next_media_cleanup_at = 0.0
    next_system_health_audit_at = 0.0
    # First retention pass ten minutes after boot, then daily.
    next_retention_at = time.monotonic() + 600
    heartbeat_path = Path(os.environ.get("KK_WORKER_HEARTBEAT_FILE", "/tmp/kk-worker-heartbeat"))
    try:
        while True:
            # Container healthcheck watches this file's mtime; a stale
            # heartbeat means the loop is stuck or dead.
            try:
                heartbeat_path.touch()
            except OSError:
                pass
            try:
                now = time.monotonic()
                if settings.ai_healthcheck_interval_seconds > 0 and now >= next_ai_healthcheck_at:
                    perform_ai_healthcheck(session_maker, settings)
                    next_ai_healthcheck_at = now + settings.ai_healthcheck_interval_seconds
                if settings.camera_rtsp_poll_interval_seconds > 0 and now >= next_camera_poll_at:
                    poll_enabled_rtsp_cameras(session_maker, settings)
                    next_camera_poll_at = now + settings.camera_rtsp_poll_interval_seconds
                if settings.media_cleanup_interval_seconds > 0 and now >= next_media_cleanup_at:
                    cleanup_expired_media_assets(session_maker, settings)
                    next_media_cleanup_at = now + settings.media_cleanup_interval_seconds
                if settings.system_metrics_interval_seconds > 0 and now >= next_system_health_audit_at:
                    perform_system_health_audit(session_maker, settings)
                    next_system_health_audit_at = now + max(60, settings.system_metrics_interval_seconds)
                if now >= next_retention_at:
                    run_retention_pass(session_maker, settings)
                    next_retention_at = now + 86400
                processed = process_due_jobs(
                    session_maker,
                    settings,
                    settings.queue_worker_batch_size,
                    "worker-loop",
                )
            except Exception:
                # One bad iteration (DB blip, queue-layer bug) must not kill
                # the process and lose the in-memory backend cooldown state.
                logger.exception("worker_loop_iteration_failed")
                time.sleep(settings.queue_worker_poll_seconds)
                continue
            if processed == 0:
                time.sleep(settings.queue_worker_poll_seconds)
    finally:
        observability.stop()
        engine.dispose()


if __name__ == "__main__":
    main()
