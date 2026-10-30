"""Painel web de administração: páginas HTML servidas pelo próprio FastAPI.

Toda ação do usuário (cadastro, configurações, comandos) é feita pelo JavaScript das páginas contra a
API JSON em /api; aqui ficam só as páginas, o fragmento das tabelas de cadastro e a documentação da API.
Todas exigem a sessão do painel (ver app/web/auth.py); sem ela, o 401 vira redirecionamento para /login (app/main.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.api.deps import get_poller, require_auth
from app.db.session import get_session
from app.poller.scheduler import Poller
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth)], include_in_schema=False)


async def _meters_context(session: AsyncSession) -> dict:
    gateways = await services.list_gateways(session)
    meters = await services.list_meters(session)
    statuses = await services.meter_statuses(session)
    counts: dict[int, int] = {}
    for m in meters:
        counts[m.gateway_id] = counts.get(m.gateway_id, 0) + 1
    return {
        "gateways": gateways, "meters": meters, "statuses": statuses,
        "meter_counts": counts, "gateway_names": {g.id: g.name for g in gateways},
        "gateway_colors": {g.id: g.color for g in gateways}, "gateway_palette": services.GATEWAY_PALETTE,
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, poller: Poller | None = Depends(get_poller)):
    return templates.TemplateResponse(request, "dashboard.html", {"page": "dashboard", "poller_enabled": poller is not None})


@router.get("/meters", response_class=HTMLResponse)
async def meters_page(request: Request, session: AsyncSession = Depends(get_session)):
    return templates.TemplateResponse(request, "meters.html", {"page": "meters", **await _meters_context(session)})


@router.get("/partials/meters", response_class=HTMLResponse)
async def partial_meters(request: Request, session: AsyncSession = Depends(get_session)):
    """Fragmento das tabelas de gateways e medidores, recarregado pelo JS depois de cada alteração."""
    return templates.TemplateResponse(request, "partials/meters_tables.html", await _meters_context(session))


@router.get("/charts", response_class=HTMLResponse)
async def charts_page(request: Request, session: AsyncSession = Depends(get_session)):
    gateways = await services.list_gateways(session)
    meters = await services.list_meters(session)
    meta = {
        "gateways": [{"id": g.id, "name": g.name, "color": g.color} for g in gateways],
        "meters": [{"id": m.id, "name": m.name, "gateway_id": m.gateway_id, "enabled": m.enabled} for m in meters],
        "metrics": services.metric_catalog(),
        "buckets": list(services.BUCKET_SECONDS),
        "bucket_seconds": services.BUCKET_SECONDS,
        "ranges": list(services.HISTORY_RANGES),
        "max_meters": services.MAX_HISTORY_METERS,
    }
    return templates.TemplateResponse(request, "charts.html", {
        "page": "charts", "gateways": gateways, "meters": meters, "meta": meta,
        "metric_groups": services.METRIC_GROUPS, "metrics": list(services.METRICS.values()),
        "max_meters": services.MAX_HISTORY_METERS, "ranges": list(services.HISTORY_RANGES),
        "buckets": list(services.BUCKET_SECONDS), "bucket_labels": services.BUCKET_LABELS,
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: AsyncSession = Depends(get_session)):
    cfg = await services.get_settings_map(session)
    return templates.TemplateResponse(request, "settings.html", {"page": "settings", "cfg": cfg})


@router.get("/commands", response_class=HTMLResponse)
async def commands_page(request: Request, session: AsyncSession = Depends(get_session)):
    entries = await services.list_commands(session, 200)
    meters = {m.id: m for m in await services.list_meters(session)}
    return templates.TemplateResponse(request, "commands.html", {"page": "commands", "entries": entries, "meters": meters})


# Documentação da API atrás do login (o FastAPI é criado com docs_url=None etc. em app/main.py).
@router.get("/openapi.json")
async def openapi_schema(request: Request):
    return JSONResponse(request.app.openapi())


@router.get("/docs", response_class=HTMLResponse)
async def swagger_docs(request: Request):
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{request.app.title} - API")


@router.get("/redoc", response_class=HTMLResponse)
async def redoc_docs(request: Request):
    return get_redoc_html(openapi_url="/openapi.json", title=f"{request.app.title} - API")
