"""Ambiente Jinja2 compartilhado pelas páginas do painel e pela página de login."""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import Undefined

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STATIC_DIR = Path(__file__).parent / "static"


def _fmt(value, digits: int = 1) -> str:
    if value is None or isinstance(value, Undefined):
        return "–"
    try:
        return f"{float(value):,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(value)


templates.env.filters["fmt"] = _fmt
# Avaliado a cada renderização: basta colocar o arquivo em app/web/static/logo.png, sem reiniciar.
templates.env.globals["has_logo"] = lambda: (STATIC_DIR / "logo.png").exists()
