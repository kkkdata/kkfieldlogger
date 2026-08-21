from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from structlog.contextvars import bind_contextvars, clear_contextvars
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes.api_v2 import router as api_v2_router
from app.api.routes.mobile import router as mobile_router
from app.api.routes.auth import router as auth_router
from app.api.routes.media import router as media_router
from app.api.routes.portal import router as portal_router
from app.api.routes.site_inquiries import router as site_inquiries_router
from app.core.config import Settings, load_settings
from app.core.errors import MobileApiError
from app.core.observability import get_logger, initialize_observability
from app.db.session import create_engine_from_settings, create_session_maker
from app.services.bootstrap import bootstrap_platform_data
from app.services.request_rate_limit import check_rate_limit
from app.services.settings import bootstrap_system_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or load_settings()
    engine = create_engine_from_settings(app_settings)
    session_maker = create_session_maker(engine)
    app_settings.photos_root.mkdir(parents=True, exist_ok=True)
    app_settings.reports_root.mkdir(parents=True, exist_ok=True)
    app_settings.media_assets_root.mkdir(parents=True, exist_ok=True)
    app_settings.media_upload_chunks_root.mkdir(parents=True, exist_ok=True)
    app_settings.media_frames_root.mkdir(parents=True, exist_ok=True)
    app_settings.media_cold_storage_root.mkdir(parents=True, exist_ok=True)
    app_settings.camera_clips_root.mkdir(parents=True, exist_ok=True)
    app_settings.logs_root.mkdir(parents=True, exist_ok=True)
    request_logger = get_logger("kkfieldlogger.request")
    error_logger = get_logger("kkfieldlogger.errors")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        observability = initialize_observability(app_settings, component="app", session_maker=session_maker)
        app.state.settings = app_settings
        app.state.engine = engine
        app.state.session_maker = session_maker
        app.state.observability = observability
        with session_maker() as db:
            bootstrap_platform_data(db, app_settings)
            bootstrap_system_settings(db, app_settings)
        yield
        observability.stop()
        engine.dispose()

    app = FastAPI(title=app_settings.app_name, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_allow_origin_list,
        allow_origin_regex=app_settings.cors_allow_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=app_settings.session_secret,
        same_site="lax",
        https_only=app_settings.public_base_url.startswith("https://"),
    )

    # Surfaces that do not exist in a private (NAS/appliance) deployment:
    # platform tenant administration, billing, public registration, and
    # tenant switching. One gate here instead of guards scattered per route.
    PRIVATE_PROFILE_BLOCKED_PREFIXES = (
        "/portal/platform",
        "/portal/billing",
        "/auth/register-company",
        "/api/v2/auth/register",
        "/api/v2/auth/switch-tenant",
    )

    def _blocked_in_private_profile(path: str) -> bool:
        if not app_settings.is_private_deployment:
            return False
        normalized = path.rstrip("/") or "/"
        if normalized == "/register":
            return True
        return normalized.startswith(PRIVATE_PROFILE_BLOCKED_PREFIXES)

    def _rate_limit_for_path(path: str) -> int | None:
        if path == "/api/v2/public/home-inquiry":
            return app_settings.site_inquiry_rate_limit_per_minute
        if path.rstrip("/") in {"/auth/register-company", "/api/v2/auth/register"}:
            return app_settings.register_rate_limit_per_minute
        if path in {"/portal/ai-center/data", "/portal/reports/data", "/portal/ip-cameras/health-summary"}:
            return app_settings.high_frequency_rate_limit_per_minute
        if path.startswith("/portal/media/"):
            return app_settings.portal_media_rate_limit_per_minute
        if path in {"/api/v2/media/upload", "/api/v2/webhooks/rtmp_recording_done"}:
            return app_settings.webhook_rate_limit_per_minute
        return None

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        clear_contextvars()
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        request.state.request_id = request_id
        request.state.request_started_at = perf_counter()
        bind_contextvars(
            request_id=request_id,
            http_method=request.method,
            http_path=request.url.path,
        )
        started_at = perf_counter()
        proxy_ip = request.client.host if request.client else "unknown"
        client_ip = request.headers.get("x-real-ip") or proxy_ip
        if _blocked_in_private_profile(request.url.path):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        limit_per_minute = _rate_limit_for_path(request.url.path)
        if limit_per_minute is not None:
            bucket_key = f"{request.url.path}:{client_ip}"
            retry_after = check_rate_limit(bucket_key, limit_per_window=limit_per_minute)
            if retry_after is not None:
                retry_after_seconds = max(1, int(round(retry_after)))
                request_logger.warning(
                    "request_rate_limited",
                    request_id=request_id,
                    method=request.method,
                    path=request.url.path,
                    client_ip=client_ip,
                    configured_limit=limit_per_minute,
                    retry_after_seconds=retry_after_seconds,
                )
                if request.url.path.startswith("/api/") or request.headers.get("accept", "").lower().find("application/json") >= 0:
                    response = JSONResponse(
                        {"detail": "Too many requests", "request_id": request_id},
                        status_code=429,
                    )
                else:
                    response = PlainTextResponse("Too many requests", status_code=429)
                response.headers["Retry-After"] = str(retry_after_seconds)
                response.headers["X-Request-ID"] = request_id
                clear_contextvars()
                return response
        response = await call_next(request)
        duration_ms = round((perf_counter() - started_at) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        request_logger.info(
            "request_complete",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            query_string=request.url.query,
            status_code=response.status_code,
            duration_ms=duration_ms,
            client_ip=client_ip,
            user_agent=request.headers.get("user-agent"),
        )
        clear_contextvars()
        return response

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", str(uuid4()))
        started_at = getattr(request.state, "request_started_at", None)
        duration_ms = round((perf_counter() - started_at) * 1000, 2) if started_at is not None else None
        error_logger.exception(
            "request_unhandled_exception",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            query_string=request.url.query,
            client_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            duration_ms=duration_ms,
            exception_type=type(exc).__name__,
            error=str(exc),
        )
        clear_contextvars()
        accept_header = (request.headers.get("accept") or "").lower()
        if request.url.path.startswith("/api/") or "application/json" in accept_header:
            response = JSONResponse(
                {"detail": "Internal server error", "request_id": request_id},
                status_code=500,
            )
        else:
            response = HTMLResponse(
                f"<html><body><h1>Internal Server Error</h1><p>Request ID: {request_id}</p></body></html>",
                status_code=500,
            )
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(MobileApiError)
    async def mobile_api_error_handler(request: Request, exc: MobileApiError):
        return JSONResponse(exc.response_body(), status_code=exc.status_code, headers=exc.headers)

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(api_v2_router)
    app.include_router(site_inquiries_router)
    app.include_router(auth_router)
    app.include_router(media_router)
    app.include_router(mobile_router)
    app.include_router(portal_router)
    return app


app = create_app()
