from __future__ import annotations

import asyncio
import contextlib
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.errors import (
    CORRELATION_ID_HEADER,
    error_body,
    http_exception_handler,
    rate_limit_exception_handler,
    unhandled_exception_body,
    validation_exception_handler,
)
from app.api.routes_admin import router as admin_router
from app.api.routes_chat import router as chat_router
from app.api.routes_config import router as config_router
from app.api.routes_machines import router as machines_router
from app.api.routes_manuals import router as manuals_router
from app.auth.routes import router as auth_router
from app.config import get_settings
from app.db import run_migrations
from app.rate_limit import limiter

WEB_DIR = Path(__file__).resolve().parent / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_settings().validate_for_startup()
    applied = run_migrations()
    if applied:
        print(f"Applied migrations: {applied}")

    # Automated corpus freshness (P1-4): only started when Drive is actually
    # configured -- get_document_source() itself would raise RuntimeError
    # otherwise, and this avoids that error firing on every tick in any
    # environment (including the test suite) that leaves Drive unconfigured.
    sync_task: asyncio.Task | None = None
    from app.ingestion import scheduler as _scheduler

    if _scheduler.is_enabled():
        sync_task = asyncio.create_task(_scheduler.run_scheduled_sync_loop())

    yield

    if sync_task is not None:
        sync_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sync_task


app = FastAPI(title="Technician Manual Assistant", version="0.1.0", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exception_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_middleware(SlowAPIMiddleware)

_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class OriginCheckMiddleware(BaseHTTPMiddleware):
    """Defense-in-depth CSRF mitigation (concern #20). Session auth is a
    cookie, so any cross-site page can trigger a state-changing request with
    the technician's credentials attached unless something checks where the
    request actually came from -- SameSite=Lax cookies already block this in
    modern browsers, but that's one setting away from silently regressing, so
    this adds an explicit, independent check.

    Only rejects requests that DO carry an Origin/Referer pointing somewhere
    else; a request with neither header (e.g. a non-browser API client, or a
    same-origin request some proxy stripped headers from) is allowed through
    rather than guessing, since blocking on absence would also break
    legitimate non-browser use of the API."""

    async def dispatch(self, request: Request, call_next):
        if request.method in _STATE_CHANGING_METHODS:
            source = request.headers.get("origin") or request.headers.get("referer")
            if source:
                source_host = urlparse(source).netloc
                if source_host and source_host != request.headers.get("host"):
                    return JSONResponse(
                        status_code=403,
                        content=error_body(request, 403, "Cross-origin request rejected."),
                    )
        return await call_next(request)


app.add_middleware(OriginCheckMiddleware)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if get_settings().app_env != "development":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    """Phase 1: every error body needs a correlation id. Set before any
    other middleware runs (this is the last @app.middleware("http") call,
    which Starlette makes the outermost layer) so even a request rejected
    by OriginCheckMiddleware above -- which never reaches a route handler --
    still gets one, and the same id that appears in the error body is also
    echoed as a response header for support/log correlation on a *success*
    response too, not just errors."""
    request.state.correlation_id = str(uuid.uuid4())
    response = await call_next(request)
    response.headers[CORRELATION_ID_HEADER] = request.state.correlation_id
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Never leak internals (stack traces, file paths, DB errors) to the client.
    settings = get_settings()
    response = unhandled_exception_body(request, settings.app_env == "development", exc)
    if response is None:
        raise exc
    return response


app.include_router(auth_router)
app.include_router(config_router)
app.include_router(machines_router)
app.include_router(chat_router)
app.include_router(manuals_router)
app.include_router(admin_router)

app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
templates = Jinja2Templates(directory=WEB_DIR / "templates")


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse(
        {
            "name": "Technician Manual Assistant",
            "short_name": "TechManual",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0b1220",
            "theme_color": "#0b1220",
            "orientation": "any",
            "icons": [
                {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        }
    )


@app.get("/service-worker.js")
def service_worker():
    from fastapi.responses import FileResponse

    return FileResponse(WEB_DIR / "static" / "js" / "service-worker.js", media_type="application/javascript")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/admin")
def admin_page(request: Request):
    return templates.TemplateResponse(request, "admin.html")
