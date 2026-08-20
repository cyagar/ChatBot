from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.routes_admin import router as admin_router
from app.api.routes_chat import router as chat_router
from app.api.routes_machines import router as machines_router
from app.api.routes_manuals import router as manuals_router
from app.auth.routes import router as auth_router
from app.config import get_settings
from app.db import run_migrations
from app.rate_limit import limiter

WEB_DIR = Path(__file__).resolve().parent / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    applied = run_migrations()
    if applied:
        print(f"Applied migrations: {applied}")
    yield


app = FastAPI(title="Technician Manual Assistant", version="0.1.0", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Never leak internals (stack traces, file paths, DB errors) to the client.
    settings = get_settings()
    if settings.app_env == "development":
        raise exc
    return JSONResponse(status_code=500, content={"detail": "An unexpected error occurred."})


app.include_router(auth_router)
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
