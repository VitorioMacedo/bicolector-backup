"""API REST (JSON). Autenticação: header X-API-Key, cookie de sessão do painel ou HTTP Basic (mesmas credenciais do painel)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.api.deps import get_poller, require_auth
from app.api.schemas import CommandOut, GatewayIn, GatewayOut, GatewayPatch, MeterIn, MeterOut, MeterPatch
from app.db.models import PollCycle
from app.db.session import get_session
from app.poller.scheduler import Poller

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


async def _reload(poller: Poller | None) -> None:
    if poller is not None:
        await poller.reload()


# ---------------------------------------------------------------- gateways
@router.get("/gateways", response_model=list[GatewayOut])
async def api_list_gateways(session: AsyncSession = Depends(get_session)):
    return await services.list_gateways(session)


@router.post("/gateways", response_model=GatewayOut, status_code=201)
async def api_create_gateway(body: GatewayIn, session: AsyncSession = Depends(get_session), poller=Depends(get_poller)):
    g = await services.create_gateway(session, **body.model_dump())
    await _reload(poller)
    return g


@router.put("/gateways/{gateway_id}", response_model=GatewayOut)
async def api_update_gateway(gateway_id: int, body: GatewayPatch, session: AsyncSession = Depends(get_session), poller=Depends(get_poller)):
    g = await services.update_gateway(session, gateway_id, **body.model_dump(exclude_unset=True))
    await _reload(poller)
    return g


@router.delete("/gateways/{gateway_id}", status_code=204)
async def api_delete_gateway(gateway_id: int, session: AsyncSession = Depends(get_session), poller=Depends(get_poller)):
    await services.delete_gateway(session, gateway_id)
    await _reload(poller)


# ---------------------------------------------------------------- medidores
@router.get("/meters", response_model=list[MeterOut])
async def api_list_meters(session: AsyncSession = Depends(get_session)):
    return await services.list_meters(session)


@router.post("/meters", response_model=MeterOut, status_code=201)
async def api_create_meter(body: MeterIn, session: AsyncSession = Depends(get_session), poller=Depends(get_poller)):
    m = await services.create_meter(session, **body.model_dump())
    await _reload(poller)
    return m


@router.put("/meters/{meter_id}", response_model=MeterOut)
async def api_update_meter(meter_id: int, body: MeterPatch, session: AsyncSession = Depends(get_session), poller=Depends(get_poller)):
    m = await services.update_meter(session, meter_id, **body.model_dump(exclude_unset=True))
    await _reload(poller)
    return m


@router.delete("/meters/{meter_id}", status_code=204)
async def api_delete_meter(meter_id: int, session: AsyncSession = Depends(get_session), poller=Depends(get_poller)):
    await services.delete_meter(session, meter_id)
    await _reload(poller)


@router.post("/meters/{meter_id}/identify", response_model=MeterOut)
async def api_identify_meter(meter_id: int, session: AsyncSession = Depends(get_session), poller=Depends(get_poller)):
    return await services.identify_meter(session, meter_id, poller)


# ---------------------------------------------------------------- comandos (reset)
@router.post("/meters/{meter_id}/reset-energy", response_model=CommandOut)
async def api_reset_energy(meter_id: int, who: str = Depends(require_auth), session: AsyncSession = Depends(get_session), poller=Depends(get_poller)):
    return await services.run_meter_command(session, meter_id, "reset_energy", who, poller)


@router.post("/meters/{meter_id}/reset-demand", response_model=CommandOut)
async def api_reset_demand(meter_id: int, who: str = Depends(require_auth), session: AsyncSession = Depends(get_session), poller=Depends(get_poller)):
    return await services.run_meter_command(session, meter_id, "reset_demand", who, poller)


@router.post("/meters/{meter_id}/reset-minmax", response_model=CommandOut)
async def api_reset_minmax(meter_id: int, who: str = Depends(require_auth), session: AsyncSession = Depends(get_session), poller=Depends(get_poller)):
    return await services.run_meter_command(session, meter_id, "reset_minmax", who, poller)


@router.get("/commands", response_model=list[CommandOut])
async def api_list_commands(limit: int = Query(100, ge=1, le=1000), session: AsyncSession = Depends(get_session)):
    return await services.list_commands(session, limit)


# ---------------------------------------------------------------- configurações
@router.get("/settings")
async def api_get_settings(session: AsyncSession = Depends(get_session)):
    return await services.get_settings_map(session)


@router.put("/settings")
async def api_put_settings(body: dict[str, str | int | float], session: AsyncSession = Depends(get_session), poller=Depends(get_poller)):
    result = await services.update_settings(session, {k: str(v) for k, v in body.items()})
    await _reload(poller)
    return result


# ---------------------------------------------------------------- status / leituras
@router.get("/status")
async def api_status(poller: Poller | None = Depends(get_poller)):
    if poller is None:
        return {"poller_enabled": False, "gateways": []}
    out = poller.state.to_dict()
    out["writer"] = {
        "pending": poller.writer.pending,
        "rows_written": poller.writer.rows_written,
        "last_flush_at": poller.writer.last_flush_at.isoformat() if poller.writer.last_flush_at else None,
        "last_error": poller.writer.last_error,
    }
    out["config"] = {
        "fast_interval_s": poller.config.fast_interval_s,
        "slow_interval_s": poller.config.slow_interval_s,
    }
    return out


@router.get("/readings/latest")
async def api_latest_readings(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(text("SELECT * FROM v_meter_latest ORDER BY machine"))).mappings().all()
    # a view devolve DATETIME2 sem fuso (UTC): marca o fuso para o JSON sair com +00:00
    return [{k: (v.replace(tzinfo=UTC) if isinstance(v, datetime) and v.tzinfo is None else v) for k, v in r.items()} for r in rows]


@router.get("/history")
async def api_history(
    metric: str = Query(..., description="grandeza (chave do catálogo: p_total, ia, vln_avg, kwh, thd_ia, ...)"),
    meter_ids: str = Query(..., description="ids dos medidores separados por vírgula (máx. 12)"),
    range_: str | None = Query(None, alias="range", description="15m, 1h, 6h, 24h, 7d ou 30d (alternativa a from/to)"),
    frm: str | None = Query(None, alias="from", description="início ISO 8601 (usado quando range não é informado)"),
    to: str | None = Query(None, description="fim ISO 8601; padrão agora"),
    bucket: str = Query("auto", description="auto ou 1s, 5s, 30s, 1min, 5min, 15min, 1h, 1d"),
    session: AsyncSession = Depends(get_session),
):
    """Série temporal de uma grandeza para até 12 medidores (média por bucket; `kwh` = consumo por bucket).
    `t` em epoch segundos UTC, um único eixo; `null` onde o medidor não tem amostra."""
    m = services.get_metric(metric)
    ids = services.parse_meter_ids(meter_ids)
    start, end = services.parse_history_window(range_, frm, to)
    plan = services.plan_for_metric(m, end - start, bucket)
    return await services.meter_history(session, m, ids, start, end, plan)


@router.get("/metrics")
async def api_metrics():
    """Catálogo de grandezas aceitas por /api/history."""
    return services.metric_catalog()


@router.get("/energy/ranking")
async def api_energy_ranking(
    range_: str | None = Query(None, alias="range", description="15m, 1h, 6h, 24h, 7d ou 30d (alternativa a from/to)"),
    frm: str | None = Query(None, alias="from", description="início ISO 8601"),
    to: str | None = Query(None, description="fim ISO 8601; padrão agora"),
    session: AsyncSession = Depends(get_session),
):
    """kWh consumido no período por todos os medidores (ordem decrescente) e o total por gateway."""
    start, end = services.parse_history_window(range_, frm, to)
    return await services.energy_ranking(session, start, end)


@router.get("/cycles/recent")
async def api_recent_cycles(minutes: int = Query(10, ge=1, le=1440), session: AsyncSession = Depends(get_session)):
    since = datetime.now(UTC) - timedelta(minutes=minutes)
    rows = (await session.execute(select(PollCycle).where(PollCycle.ts >= since).order_by(PollCycle.ts.desc()).limit(5000))).scalars()
    return [
        {"ts": r.ts, "gateway_id": r.gateway_id, "duration_ms": r.duration_ms, "interval_ms": r.interval_ms,
         "meters_ok": r.meters_ok, "meters_failed": r.meters_failed, "slow_reads": r.slow_reads}
        for r in rows
    ]
