"""Dependências comuns: autenticação (chave de API, cookie de sessão do painel ou HTTP Basic) e acesso ao coletor."""
from __future__ import annotations

import secrets
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader, HTTPBasic, HTTPBasicCredentials

from app.auth.passwords import verify_password
from app.auth.session import COOKIE_NAME, read_token
from app.config import get_settings
from app.poller.scheduler import Poller

_basic = HTTPBasic(auto_error=False)
_api_key = APIKeyHeader(name="X-API-Key", auto_error=False)
API_KEY_USER = "api-key"


def safe_next(value: str | None, default: str = "/") -> str:
    """Só aceita destino relativo ao próprio site (evita open redirect no `next` do login)."""
    if value and value.startswith("/") and not value.startswith(("//", "/\\")) and "\n" not in value and "\r" not in value:
        return value
    return default


async def current_user(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_basic),
    api_key: str | None = Depends(_api_key),
) -> str | None:
    """Quem está autenticado ('api-key', ou o usuário do cookie/Basic) ou None. Nunca devolve desafio Basic ao navegador."""
    s = get_settings()
    who: str | None = None
    if api_key and secrets.compare_digest(api_key.encode(), s.api_key.encode()):
        who = API_KEY_USER
    elif (user := read_token(request.cookies.get(COOKIE_NAME))) and secrets.compare_digest(user.encode(), s.admin_user.encode()):
        who = user
    elif credentials and secrets.compare_digest(credentials.username.encode(), s.admin_user.encode()) and verify_password(credentials.password):
        who = credentials.username
    request.state.user = who
    return who


async def require_auth(who: str | None = Depends(current_user)) -> str:
    """401 sem WWW-Authenticate (o navegador não abre popup; integrações usam X-API-Key).

    Nas páginas HTML (fora de /api) o handler em app/main.py converte esse 401 em redirecionamento para /login.
    """
    if who is None:
        raise HTTPException(status_code=401, detail="não autenticado")
    return who


def login_redirect_url(request: Request) -> str:
    """URL da página de login que volta para a página pedida depois de entrar."""
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return "/login?next=" + quote(safe_next(target), safe="/")


def get_poller(request: Request) -> Poller | None:
    return getattr(request.app.state, "poller", None)
