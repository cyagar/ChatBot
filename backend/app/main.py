from __future__ import annotations

import asyncio
import contextlib
import os
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
from app.api.routes_config import _corpus_status
from app.api.routes_config import router as config_router
from app.api.routes_machines import router as machines_router
from app.api.routes_manuals import router as manuals_router
from app.auth.routes import router as auth_router
from app.config import get_settings
from app.db import get_conn, run_migrations
from app.rate_limit import limiter

WEB_DIR = Path(__file__).resolve().parent / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_settings().validate_for_startup()
    applied = run_migrations()
    if applied:
        print(f"Applied migrations: {applied}")

    from app.ingestion.pipeline import recover_interrupted_runs

    recovered = recover_interrupted_runs()
    if recovered:
        print(f"Marked {recovered} interrupted ingestion run(s) as failed")

    # The automated corpus-freshness loop only starts when Drive is actually
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


def interactive_docs_urls(app_env: str) -> dict[str, str | None]:
    """Swagger UI, ReDoc and the raw schema are only served in development --
    on a deployed instance they hand anyone on the internet a complete map of
    every route, including the administrator API. The committed
    backend/openapi.json (scripts/export_openapi.py, via app.openapi()) is
    unaffected."""
    enabled = app_env == "development"
    return {
        "docs_url": "/docs" if enabled else None,
        "redoc_url": "/redoc" if enabled else None,
        "openapi_url": "/openapi.json" if enabled else None,
    }


app = FastAPI(
    title="Technician Manual Assistant", version="0.1.0", lifespan=lifespan,
    **interactive_docs_urls(get_settings().app_env),
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exception_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_middleware(SlowAPIMiddleware)

_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class OriginCheckMiddleware(BaseHTTPMiddleware):
    """Defense-in-depth CSRF mitigation. Session auth is a
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
    # The only served UI (admin.html -- there is no technician-facing web
    # UI; technicians use the Android app) only ever loads same-origin
    # external <script src="/static/..."> with no inline script/style
    # anywhere in the template, so this can be strict -- 'self' only, no
    # 'unsafe-inline'.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    )
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    if get_settings().app_env != "development":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    """Every error body needs a correlation id. Set before any
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


@app.get("/healthz")
def healthz():
    return {"ok": True}


_STUCK_INGESTION_HOURS = 2


def _corpus_readiness(settings) -> tuple[str, str]:
    """(corpus, ingestion) for /readyz. corpus: "ok", "degraded" (usable but
    stale), "unusable" (nothing retrievable) or "error" (could not be
    determined). ingestion: "ok" or "stuck" (a run has claimed to be running
    for hours)."""
    try:
        with get_conn() as conn:
            retrievable = conn.execute(
                "SELECT 1 FROM documents d JOIN document_machines dm ON dm.document_id = d.id "
                "WHERE d.review_status = 'approved' AND d.deactivated_at IS NULL AND d.is_current_revision "
                "AND dm.review_status = 'approved' LIMIT 1"
            ).fetchone()
            stuck = conn.execute(
                "SELECT 1 FROM ingestion_runs WHERE status = 'running' "
                "AND started_at < now() - make_interval(hours => %s) LIMIT 1",
                (_STUCK_INGESTION_HOURS,),
            ).fetchone()
    except Exception:
        return "error", "ok"
    ingestion = "stuck" if stuck else "ok"
    if retrievable is None:
        return "unusable", ingestion
    status, _ = _corpus_status(settings)
    return status, ingestion


@app.get("/readyz")
def readyz():
    """Readiness: can this instance serve a cited answer right now. Fails
    (503) when the database or storage is unavailable, when no approved,
    current, machine-linked document is retrievable, or when that cannot be
    determined. A corpus that is usable but stale, or an ingestion run that
    looks stuck, is reported as "degraded"/"stuck" with HTTP 200 so a load
    balancer keeps serving while alerting can act on the body.

    /healthz is the liveness probe (the process is up). No auth, and only
    booleans and status strings are returned, never a filename or manual
    title, so this is safe to expose publicly."""
    settings = get_settings()

    database_ok = True
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1")
    except Exception:
        database_ok = False

    storage_dir = settings.local_storage_dir_resolved
    storage_ok = storage_dir.is_dir() and os.access(storage_dir, os.R_OK)

    corpus, ingestion = _corpus_readiness(settings) if database_ok else ("error", "ok")

    ok = database_ok and storage_ok and corpus in ("ok", "degraded")
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "ok": ok,
            "database": "ok" if database_ok else "error",
            "storage": "ok" if storage_ok else "error",
            "corpus": corpus,
            "ingestion": ingestion,
        },
    )


@app.get("/admin")
def admin_page(request: Request):
    return templates.TemplateResponse(request, "admin.html")


@app.get("/invite")
def invite_page(request: Request):
    """Minimal HTML redemption page for an admin-issued invitation link:
    reads token/email from the query string and lets the recipient choose a
    password by calling the existing POST /api/auth/register directly -- see
    invite.html. After account creation, the technician signs in from the
    Android app; this page does nothing beyond registration itself."""
    return templates.TemplateResponse(request, "invite.html")
