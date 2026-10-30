"""Cookie de sessão: token assinado (itsdangerous) com usuário e data de emissão; a validade é checada na leitura."""
from __future__ import annotations

import logging
import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.auth.passwords import password_fingerprint
from app.config import get_settings

COOKIE_NAME = "bic_session"
log = logging.getLogger("app.auth")
_runtime_secret: str | None = None


def secret_key() -> str:
    global _runtime_secret
    s = get_settings()
    if s.secret_key:
        return s.secret_key
    if _runtime_secret is None:
        _runtime_secret = secrets.token_urlsafe(48)
        log.warning("SECRET_KEY não definido: chave gerada nesta subida, as sessões expiram ao reiniciar")
    return _runtime_secret


def _serializer() -> URLSafeTimedSerializer:
    # o salt muda com a senha: trocar ADMIN_PASSWORD(_HASH) invalida todos os cookies emitidos
    return URLSafeTimedSerializer(secret_key(), salt="bic-session:" + password_fingerprint())


def make_token(user: str, max_age_s: int) -> str:
    return _serializer().dumps({"u": user, "a": max_age_s})


def read_token(token: str | None) -> str | None:
    """Usuário do token, ou None se ausente, adulterado ou vencido."""
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=_max_age(token))
    except (BadSignature, SignatureExpired, ValueError):
        return None
    user = data.get("u") if isinstance(data, dict) else None
    return user if isinstance(user, str) and user else None


def _max_age(token: str) -> int:
    """A validade vai dentro do próprio token (sessão normal x 'manter conectado'); lê sem checar prazo, a assinatura decide."""
    try:
        data = _serializer().loads(token)
    except (BadSignature, ValueError):
        return 0
    s = get_settings()
    cap = max(s.session_hours * 3600, s.session_remember_days * 86400)
    age = data.get("a") if isinstance(data, dict) else None
    return min(int(age), cap) if isinstance(age, int) and age > 0 else s.session_hours * 3600
