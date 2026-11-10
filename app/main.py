import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import scheduler
from app.config import APP_VERSION, SECRET_KEY, SESSION_COOKIE_NAME, SESSION_HTTPS_ONLY, SESSION_MAX_AGE
from app.database import init_db
from app.event_log import DBLogHandler
from app.routers import auth, backups, containers, jobs, logs, netpulse, schedules, settings

# dbm.* loggers have no handlers in uvicorn's default config (only uvicorn.* are
# configured). Without this, all INFO messages from dbm.remote_cleanup etc. are
# silently dropped by Python's lastResort handler which only passes WARNING+.
_dbm_handler = logging.StreamHandler(sys.stdout)
_dbm_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s - %(message)s"))
_dbm_logger = logging.getLogger("dbm")
_dbm_logger.addHandler(_dbm_handler)
# Also write to the persistent DB log so entries appear in the web UI
_db_log_handler = DBLogHandler()
_db_log_handler.setLevel(logging.INFO)
_dbm_logger.addHandler(_db_log_handler)
_dbm_logger.setLevel(logging.INFO)
_dbm_logger.propagate = False

STATIC_DIR = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="Docker Backup Manager", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie=SESSION_COOKIE_NAME,
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=SESSION_HTTPS_ONLY,
)

app.include_router(auth.router)
app.include_router(containers.router)
app.include_router(backups.router)
app.include_router(schedules.router)
app.include_router(settings.router)
app.include_router(jobs.router)
app.include_router(logs.router)
app.include_router(netpulse.router)

app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def security_and_cache_headers(request, call_next):
    # CSRF: the session cookie is SameSite=Lax, which still lets *same-site*
    # pages (e.g. another web app on the same NAS, different port) send it
    # along. Browsers label every request with Sec-Fetch-Site; anything that
    # changes state must come from this app's own origin. Requests without
    # the header (curl, scripts, old browsers) are not affected.
    if (request.url.path.startswith("/api/") and request.method in _UNSAFE_METHODS
            and request.headers.get("sec-fetch-site") in ("cross-site", "same-site")):
        return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)

    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        # Nach einem Update sofort die neue Oberfläche: der Browser fragt jedes Mal kurz nach (sonst 304)
        response.headers.setdefault("Cache-Control", "no-cache")
    else:
        response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


_INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace(
    "/assets/js/app.js", f"/assets/js/app.js?v={APP_VERSION}").replace(
    "/assets/css/style.css", f"/assets/css/style.css?v={APP_VERSION}")


@app.get("/{full_path:path}")
def spa(full_path: str):
    # Versionsnummer an Skript/Stylesheet: neue Version = neue Adresse = kein alter Stand aus dem Cache
    return HTMLResponse(_INDEX_HTML)


