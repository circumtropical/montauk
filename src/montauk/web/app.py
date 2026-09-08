"""FastAPI application factory for the Montauk dashboard (spec 25).

Server-rendered Jinja2, no SPA build step (ADR 0002). Auth is the
dashboard's own password/session layer (spec 25.1); the MCP OAuth flow is
a separate increment.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware

from ..db.crypto import SecretBox
from ..db.engine import create_db_engine, session_factory
from ..services.auth import SESSION_COOKIE, LoginThrottle, resolve_session
from ..services.workspace import is_initialized

_HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))


def _humantime(value: dt.datetime | None) -> str:
    if value is None:
        return "never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.strftime("%Y-%m-%d %H:%M UTC")


TEMPLATES.env.filters["humantime"] = _humantime


class AppState:
    def __init__(self, factory: sessionmaker[Session], *, throttle: LoginThrottle, secure_cookies: bool):
        self.session_factory = factory
        self.throttle = throttle
        self.secure_cookies = secure_cookies
        self._secret_box: SecretBox | None = None
        self._secret_box_loaded = False

    @property
    def secret_box(self) -> SecretBox | None:
        """The AES-GCM SecretBox, or None when MONTAUK_MASTER_KEY is unset.
        Loaded lazily so the dashboard still boots without a master key --
        provider API-key storage is simply unavailable until one is set."""
        if not self._secret_box_loaded:
            from ..db.crypto import MasterKeyMissing

            try:
                self._secret_box = SecretBox()
            except MasterKeyMissing:
                self._secret_box = None
            self._secret_box_loaded = True
        return self._secret_box


def get_state(request: Request) -> AppState:
    return request.app.state.montauk


def db_session(request: Request) -> Iterator[Session]:
    factory = get_state(request).session_factory
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            # script-src stays strict ('self' only, no inline). style-src
            # allows inline style attributes for small layout tweaks in the
            # templates -- no script execution risk.
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        return response


def create_app(
    factory: sessionmaker[Session] | None = None,
    *,
    database_url: str | None = None,
    throttle: LoginThrottle | None = None,
    secure_cookies: bool = True,
) -> FastAPI:
    if factory is None:
        factory = session_factory(create_db_engine(database_url))

    app = FastAPI(title="Montauk", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.montauk = AppState(factory, throttle=throttle or LoginThrottle(), secure_cookies=secure_cookies)
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    from .routes import auth, exports, home, people, settings, setup

    app.include_router(setup.router)
    app.include_router(auth.router)
    app.include_router(home.router)
    app.include_router(people.router)
    app.include_router(exports.router)
    app.include_router(settings.router)

    @app.middleware("http")
    async def _guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Route everything through the first-run wizard until an owner
        exists, and to /login until authenticated. Static assets, the
        wizard, and the login form are exempt."""
        path = request.url.path
        if path.startswith("/static/") or path in ("/healthz", "/favicon.ico"):
            return await call_next(request)
        exempt = path.startswith("/setup") or path == "/login"
        factory = request.app.state.montauk.session_factory
        with factory() as s:
            initialized = is_initialized(s)
            auth_ctx = resolve_session(s, request.cookies.get(SESSION_COOKIE)) if initialized else None
            s.commit()

        if not initialized and not path.startswith("/setup"):
            return RedirectResponse("/setup", status_code=303)
        if initialized and path.startswith("/setup"):
            return RedirectResponse("/", status_code=303)
        if initialized and auth_ctx is None and not exempt:
            return RedirectResponse("/login", status_code=303)

        request.state.auth = auth_ctx
        return await call_next(request)

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        import sqlalchemy as sa

        try:
            with factory() as s:
                s.execute(sa.text("SELECT 1"))
            db_ok = True
        except Exception:  # noqa: BLE001
            db_ok = False
        return {"status": "ok" if db_ok else "degraded", "database": db_ok}

    @app.exception_handler(404)
    async def _not_found(request: Request, exc: object):  # type: ignore[no-untyped-def]
        return TEMPLATES.TemplateResponse(
            request, "error.html", {"code": 404, "message": "Not found"}, status_code=404
        )

    return app
