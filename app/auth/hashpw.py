"""Gera o ADMIN_PASSWORD_HASH para o .env: `uv run python -m app.auth.hashpw`."""
from __future__ import annotations

import getpass
import sys

from argon2 import PasswordHasher


def main() -> int:
    pw = getpass.getpass("Senha do admin: ")
    if len(pw) < 8:
        print("use pelo menos 8 caracteres", file=sys.stderr)
        return 1
    if pw != getpass.getpass("Repita a senha: "):
        print("as senhas não conferem", file=sys.stderr)
        return 1
    print("\nCole no .env (e remova ADMIN_PASSWORD):\n")
    print(f"ADMIN_PASSWORD_HASH={PasswordHasher().hash(pw)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
