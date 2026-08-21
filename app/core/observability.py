from __future__ import annotations

import logging
import os
import platform
import socket
import sys
import threading
from dataclasses import dataclass
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import psutil
import structlog
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker
from structlog.contextvars import bind_contextvars

from app.core.config import Settings


def _add_process_context(settings: Settings, component: str):
    hostname = socket.gethostname()
    pid = os.getpid()
    python_version = platform.python_version()
    platform_name = platform.platform()

    def processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        event_dict.setdefault("environment", settings.environment)
        event_dict.setdefault("component", component)
        event_dict.setdefault("hostname", hostname)
        event_dict.setdefault("pid", pid)
        event_dict.setdefault("python_version", python_version)
        event_dict.setdefault("platform", platform_name)
        return event_dict

    return processor


def _build_processors(settings: Settings, component: str) -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _add_process_context(settings, component),
    ]


def _reset_standard_loggers() -> None:
    for logger_name in ["uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"]:
        logger = logging.getLogger(logger_name)
        logger.handlers = []
        logger.propagate = True


def _build_handler(log_path: Path | None, processors: list[Any], *, retention_days: int) -> logging.Handler:
    if log_path is None:
        handler = logging.StreamHandler(sys.stdout)
    else:
        handler = TimedRotatingFileHandler(
            filename=log_path,
            when="midnight",
            backupCount=retention_days,
            utc=True,
            encoding="utf-8",
        )
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=processors,
        )
    )
    return handler


def configure_logging(settings: Settings, *, component: str) -> None:
    logs_root = settings.logs_root
    logs_root.mkdir(parents=True, exist_ok=True)
    processors = _build_processors(settings, component)
    structlog.configure(
        processors=[*processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    root_logger.addHandler(_build_handler(None, processors, retention_days=settings.log_retention_days))
    root_logger.addHandler(
        _build_handler(logs_root / f"{component}.log", processors, retention_days=settings.log_retention_days)
    )
    logging.captureWarnings(True)
    _reset_standard_loggers()
    bind_contextvars(component=component, environment=settings.environment)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def _safe_round(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def _queue_metrics(db: Session) -> dict[str, int]:
    from app.models import TaskJob, TaskStatus

    rows = db.execute(
        select(TaskJob.status, func.count())
        .group_by(TaskJob.status)
    ).all()
    counts = {
        "queue_queued_jobs": 0,
        "queue_running_jobs": 0,
        "queue_failed_jobs": 0,
        "queue_dead_letter_jobs": 0,
        "queue_completed_jobs": 0,
    }
    for status_value, count in rows:
        status_name = status_value.value if hasattr(status_value, "value") else str(status_value)
        counts[f"queue_{status_name}_jobs"] = int(count)
    counts["queue_active_jobs"] = (
        counts.get("queue_queued_jobs", 0)
        + counts.get("queue_running_jobs", 0)
        + counts.get("queue_failed_jobs", 0)
    )
    return counts


def _postgres_connection_count(db: Session) -> int | None:
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return None
    return db.scalar(text("SELECT COUNT(*) FROM pg_stat_activity WHERE datname = current_database()"))


def collect_system_snapshot(settings: Settings, session_maker: sessionmaker[Session] | None = None) -> dict[str, Any]:
    process = psutil.Process()
    snapshot: dict[str, Any] = {
        "cpu_percent": _safe_round(psutil.cpu_percent(interval=None)),
        "memory_percent": _safe_round(psutil.virtual_memory().percent),
        "memory_used_mb": _safe_round(psutil.virtual_memory().used / (1024 * 1024)),
        "process_rss_mb": _safe_round(process.memory_info().rss / (1024 * 1024)),
    }

    disk_target = settings.media_root_path if settings.media_root_path.exists() else Path.cwd()
    disk_usage = psutil.disk_usage(str(disk_target))
    snapshot.update(
        {
            "disk_free_gb": _safe_round(disk_usage.free / (1024 * 1024 * 1024)),
            "disk_used_percent": _safe_round(disk_usage.percent),
            "disk_path": str(disk_target),
        }
    )

    if session_maker is None:
        return snapshot

    with session_maker() as db:
        snapshot["postgres_connection_count"] = _postgres_connection_count(db)
        snapshot.update(_queue_metrics(db))
    return snapshot


@dataclass
class ObservabilityRuntime:
    settings: Settings
    component: str
    session_maker: sessionmaker[Session] | None
    logger: structlog.stdlib.BoundLogger
    _stop_event: threading.Event
    _thread: threading.Thread | None = None

    def start(self) -> "ObservabilityRuntime":
        if self.settings.system_metrics_interval_seconds <= 0:
            return self
        if self.session_maker is None:
            return self
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._run_metrics_loop,
            name=f"{self.component}-observability",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run_metrics_loop(self) -> None:
        while not self._stop_event.wait(self.settings.system_metrics_interval_seconds):
            try:
                snapshot = collect_system_snapshot(self.settings, self.session_maker)
                self.logger.info("system_load_snapshot", **snapshot)
            except Exception:
                self.logger.exception("system_load_snapshot_failed")


def initialize_observability(
    settings: Settings,
    *,
    component: str,
    session_maker: sessionmaker[Session] | None = None,
) -> ObservabilityRuntime:
    configure_logging(settings, component=component)
    psutil.cpu_percent(interval=None)
    logger = get_logger(f"kkfieldlogger.{component}")
    logger.info(
        "observability_configured",
        log_dir=str(settings.logs_root),
        log_level=settings.log_level.upper(),
        retention_days=settings.log_retention_days,
        metrics_interval_seconds=settings.system_metrics_interval_seconds,
        otel_enabled=settings.otel_enabled,
        otel_endpoint=settings.otel_endpoint,
    )
    runtime = ObservabilityRuntime(
        settings=settings,
        component=component,
        session_maker=session_maker,
        logger=logger,
        _stop_event=threading.Event(),
    )
    runtime.start()
    return runtime
