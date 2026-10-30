"""Ponto de entrada: FastAPI + coletor Modbus rodando no mesmo processo."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.deps import login_redirect_url
from app.api.routes import router as api_router
from app.config import get_settings
from app.db.rollup import Maintenance
from app.db.schema import init_schema
from app.db.session import dispose_engine, get_engine, get_session_factory
from app.poller.scheduler import Poller
from app.web.auth import router as auth_router
from app.web.routes import router as web_router

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    logging.basicConfig(level=s.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("pymodbus").setLevel(logging.WARNING)
    _warn_weak_credentials(s)
    engine = get_engine()
    await init_schema(engine)
    poller: Poller | None = None
    maintenance: Maintenance | None = None
    if s.poller_enabled:
        poller = Poller(engine, get_session_factory(), timeout=s.modbus_timeout_s, retries=s.modbus_retries, config_refresh_s=s.config_refresh_s)
        await poller.start()
        # agregados de resumo e retenção: junto do coletor, para rodar numa instância só
        maintenance = Maintenance(engine, lambda: poller.config.raw_retention_days)
        maintenance.start()
    else:
        log.warning("coletor desabilitado (POLLER_ENABLED=false)")
    app.state.poller = poller
    app.state.maintenance = maintenance
    try:
        yield
    finally:
        if maintenance is not None:
            await maintenance.stop()
        if poller is not None:
            await poller.stop()
        await dispose_engine()


def _warn_weak_credentials(s) -> None:
    if not s.admin_password_hash:
        log.warning("ADMIN_PASSWORD em texto puro no .env; gere um hash com `uv run python -m app.auth.hashpw` (ADMIN_PASSWORD_HASH)")
        if s.admin_password == "admin":
            log.warning("senha do painel é a padrão ('admin'): troque ADMIN_PASSWORD no .env")
    if s.api_key == "dev-key":
        log.warning("API_KEY é a padrão ('dev-key'): troque no .env")
    if not s.secret_key:
        log.warning("SECRET_KEY não definido no .env: as sessões do painel caem a cada reinício")


# /docs, /redoc e /openapi.json são servidos pelo router web, atrás do login (app/web/routes.py)
app = FastAPI(title="bic — coletor PM2230", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(api_router)
app.include_router(auth_router)
app.include_router(web_router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")



@app.exception_handler(HTTPException)
async def _http_exception(request: Request, exc: HTTPException):
    """Páginas HTML sem sessão vão para o login (a API JSON continua respondendo 401)."""
    if exc.status_code == 401 and request.method == "GET" and not request.url.path.startswith("/api/"):
        return RedirectResponse(login_redirect_url(request), status_code=303, headers={"Cache-Control": "no-store"})
    return await http_exception_handler(request, exc)


_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'; object-src 'none'"
)
# Swagger UI e ReDoc carregam seus assets do jsDelivr e usam script inline
_CSP_DOCS = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.jsdelivr.net; connect-src 'self'; worker-src 'self' blob:; "
    "font-src 'self' https://fonts.gstatic.com; frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
)


def _same_site(request: Request) -> bool:
    """Proteção CSRF: em métodos que alteram estado, Origin (ou Referer) precisa bater com o Host da requisição.

    Navegadores sempre mandam Origin em POST cross-site, então um formulário malicioso em outro site não passa.
    Chamadas de integrações (X-API-Key, curl) normalmente não têm Origin e são aceitas.
    """
    origin = request.headers.get("origin")
    if origin is None:
        referer = request.headers.get("referer")
        if referer is None:
            return True
        origin = referer
    if origin == "null":
        return False
    return urlsplit(origin).netloc.lower() == (request.headers.get("host") or "").lower()


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if request.method in _UNSAFE and not request.headers.get("x-api-key") and not _same_site(request):
        log.warning("requisição %s %s bloqueada: origem %r não confere com host %r", request.method, request.url.path,
                    request.headers.get("origin") or request.headers.get("referer"), request.headers.get("host"))
        return JSONResponse({"detail": "origem da requisição não permitida"}, status_code=403)
    response = await call_next(request)
    h = response.headers
    path = request.url.path
    h.setdefault("Content-Security-Policy", _CSP_DOCS if path in ("/docs", "/redoc") else _CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if path.startswith("/api/") or path == "/login":
        h.setdefault("Cache-Control", "no-store")
    if get_settings().cookie_secure:
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.get("/healthz", include_in_schema=False)
async def healthz():
    poller: Poller | None = getattr(app.state, "poller", None)
    return {"ok": True, "poller": poller is not None and poller.state.poller_enabled}
