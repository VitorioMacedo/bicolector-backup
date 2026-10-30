"""Verificação da senha do admin: hash Argon2id (ADMIN_PASSWORD_HASH) ou, na falta dele, texto puro."""
from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from app.config import get_settings

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str) -> bool:
    s = get_settings()
    if s.admin_password_hash:
        try:
            return _hasher.verify(s.admin_password_hash, plain)
        except (VerifyMismatchError, VerificationError, ValueError):
            return False
    return secrets.compare_digest(plain.encode(), s.admin_password.encode())


def password_fingerprint() -> str:
    """Identifica a senha vigente sem revelá-la; entra no salt do token para que trocar a senha derrube as sessões."""
    s = get_settings()
    material = s.admin_password_hash or s.admin_password
    return hashlib.sha256(material.encode()).hexdigest()
