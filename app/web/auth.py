"""Página de login e logout do painel (sessão por cookie assinado). Único router sem exigência de autenticação."""
from __future__ import annotations

import asyncio
import logging
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.api.deps import API_KEY_USER, current_user, safe_next
from app.auth.passwords import verify_password
from app.auth.session import COOKIE_NAME, make_token
from app.auth.throttle import throttle
from app.config import get_settings
from app.web.templating import templates

router = APIRouter(include_in_schema=False)
log = logging.getLogger("app.auth")


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _render(request: Request, *, status: int = 200, error: str | None = None, username: str = "", next_url: str = "/") -> Response:
    resp = templates.TemplateResponse(
        request, "login.html", {"error": error, "username": username, "next": next_url}, status_code=status,
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str | None = None, who: str | None = Depends(current_user)):
    target = safe_next(next)
    if who and who != API_KEY_USER:
        return RedirectResponse(target, status_code=303)
    return _render(request, next_url=target)


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    remember: str | None = Form(None),
    next: str = Form("/"),
):
    s = get_settings()
    ip = _ip(request)
    target = safe_next(next)
    username = username.strip()
    if (wait := throttle.blocked_for(ip)):
        minutes = -(-wait // 60)
        log.warning("login bloqueado ip=%s (%ss restantes)", ip, wait)
        return _render(request, status=429, error=f"Muitas tentativas. Aguarde {minutes} min e tente de novo.", username=username, next_url=target)

    ok = secrets.compare_digest(username.encode(), s.admin_user.encode()) and verify_password(password)
    if not ok:
        blocked = throttle.record_failure(ip)
        log.warning("login falhou user=%r ip=%s%s", username, ip, " — bloqueado" if blocked else "")
        await asyncio.sleep(0.5)  # encarece a enumeração sem travar o servidor
        return _render(request, status=401, error="Usuário ou senha inválidos.", username=username, next_url=target)

    throttle.reset(ip)
    max_age = s.session_remember_days * 86400 if remember else s.session_hours * 3600
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(COOKIE_NAME, make_token(username, max_age), max_age=max_age, httponly=True, samesite="lax", secure=s.cookie_secure, path="/")
    resp.headers["Cache-Control"] = "no-store"
    log.info("login ok user=%s ip=%s%s", username, ip, " (manter conectado)" if remember else "")
    return resp


@router.post("/logout")
async def logout(request: Request, who: str | None = Depends(current_user)):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=get_settings().cookie_secure)
    log.info("logout user=%s ip=%s", who, _ip(request))
    return resp
