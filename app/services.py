"""Operações compartilhadas entre a API REST e o painel web (cadastro, configurações, comandos)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import available_timezones

from fastapi import HTTPException
from sqlalchemy import bindparam, delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AppSetting, CommandLog, Gateway, Meter, MeterStatus, UtcDateTime
from app.db.schema import DEFAULT_SETTINGS
from app.tz import windows_timezone
from app.modbus import registers as R
from app.modbus.client import GatewayClient
from app.modbus.commands import CommandResult, read_identification, send_command
from app.poller.scheduler import Poller

log = logging.getLogger(__name__)

COMMANDS = {
    "reset_energy": ("reset_command_code", R.COMMAND_RESET_ENERGY_DEFAULT),
    "reset_demand": ("reset_demand_code", R.COMMAND_RESET_DEMAND_DEFAULT),
    "reset_minmax": ("reset_minmax_code", R.COMMAND_RESET_MINMAX_DEFAULT),
}

SETTING_RULES: dict[str, tuple[type, float | None, float | None]] = {
    # chave: (tipo, mínimo, máximo)
    "fast_interval_s": (float, 0.2, 3600),
    "slow_interval_s": (float, 1, 86400),
    "raw_retention_days": (int, 1, 36500),
    "offline_after_failures": (int, 1, 1000),
    "reset_command_code": (int, 0, 65535),
    "reset_demand_code": (int, 0, 65535),
    "reset_minmax_code": (int, 0, 65535),
    "reset_password": (int, 0, 9999),
    "reset_layout": (str, None, None),
    "display_timezone": (str, None, None),
}


# ----------------------------------------------------------------------------- gateways
# Paleta categórica validada (dataviz: pares adjacentes distinguíveis com e sem daltonismo), na ordem em que os
# gateways recebem cor. Validar de novo com scripts/validate_palette.js do skill dataviz se mudar a ordem.
GATEWAY_PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def next_gateway_color(used: list[str | None]) -> str:
    """Primeira cor da paleta ainda não usada; quando todas estão em uso, cicla pela quantidade de gateways."""
    taken = {c.lower() for c in used if c}
    for c in GATEWAY_PALETTE:
        if c not in taken:
            return c
    return GATEWAY_PALETTE[len(used) % len(GATEWAY_PALETTE)]


def _check_color(color: str | None) -> str | None:
    if color is None or color == "":
        return None
    if not _COLOR_RE.match(color):
        raise HTTPException(422, "color deve ser uma cor no formato #rrggbb")
    return color.lower()


async def list_gateways(session: AsyncSession) -> list[Gateway]:
    return list((await session.execute(select(Gateway).order_by(Gateway.id))).scalars())


async def create_gateway(session: AsyncSession, name: str, host: str, port: int = 502, fast_block: str = "full",
                         enabled: bool = True, color: str | None = None) -> Gateway:
    _check_block(fast_block)
    color = _check_color(color) or next_gateway_color([g.color for g in await list_gateways(session)])
    g = Gateway(name=name.strip(), host=host.strip(), port=port, fast_block=fast_block, enabled=enabled, color=color)
    session.add(g)
    await session.commit()
    await session.refresh(g)
    return g


async def update_gateway(session: AsyncSession, gateway_id: int, **fields) -> Gateway:
    g = await session.get(Gateway, gateway_id)
    if g is None:
        raise HTTPException(404, "gateway não encontrado")
    if "fast_block" in fields:
        _check_block(fields["fast_block"])
    if "color" in fields:
        fields["color"] = _check_color(fields["color"])
    for k, v in fields.items():
        if v is not None:
            setattr(g, k, v.strip() if isinstance(v, str) else v)
    await session.commit()
    await session.refresh(g)
    return g


async def delete_gateway(session: AsyncSession, gateway_id: int) -> None:
    await session.execute(delete(Gateway).where(Gateway.id == gateway_id))
    await session.commit()


def _check_block(fast_block: str) -> None:
    if fast_block not in R.FAST_BLOCKS:
        raise HTTPException(422, f"fast_block deve ser um de {sorted(R.FAST_BLOCKS)}")


# ----------------------------------------------------------------------------- medidores
async def list_meters(session: AsyncSession) -> list[Meter]:
    return list((await session.execute(select(Meter).order_by(Meter.gateway_id, Meter.unit_id))).scalars())


async def create_meter(session: AsyncSession, gateway_id: int, name: str, unit_id: int, enabled: bool = True) -> Meter:
    if not 1 <= unit_id <= 247:
        raise HTTPException(422, "unit_id deve estar entre 1 e 247")
    if await session.get(Gateway, gateway_id) is None:
        raise HTTPException(404, "gateway não encontrado")
    dup = await session.scalar(select(Meter).where(Meter.gateway_id == gateway_id, Meter.unit_id == unit_id))
    if dup:
        raise HTTPException(409, f"já existe um medidor com unit_id {unit_id} nesse gateway ({dup.name})")
    m = Meter(gateway_id=gateway_id, name=name.strip(), unit_id=unit_id, enabled=enabled)
    session.add(m)
    await session.commit()
    await session.refresh(m)
    return m


async def update_meter(session: AsyncSession, meter_id: int, **fields) -> Meter:
    m = await session.get(Meter, meter_id)
    if m is None:
        raise HTTPException(404, "medidor não encontrado")
    for k, v in fields.items():
        if v is not None:
            setattr(m, k, v.strip() if isinstance(v, str) else v)
    await session.commit()
    await session.refresh(m)
    return m


async def delete_meter(session: AsyncSession, meter_id: int) -> None:
    await session.execute(delete(Meter).where(Meter.id == meter_id))
    await session.commit()


async def get_meter_or_404(session: AsyncSession, meter_id: int) -> Meter:
    m = await session.get(Meter, meter_id)
    if m is None:
        raise HTTPException(404, "medidor não encontrado")
    return m


async def meter_statuses(session: AsyncSession) -> dict[int, MeterStatus]:
    return {s.meter_id: s for s in (await session.execute(select(MeterStatus))).scalars()}


# ----------------------------------------------------------------------------- configurações
async def get_settings_map(session: AsyncSession) -> dict[str, str]:
    rows = {r.key: r.value for r in (await session.execute(select(AppSetting))).scalars()}
    return {**DEFAULT_SETTINGS, **rows}


async def update_settings(session: AsyncSession, values: dict[str, str]) -> dict[str, str]:
    for key, raw in values.items():
        if key not in SETTING_RULES:
            raise HTTPException(422, f"configuração desconhecida: {key}")
        typ, lo, hi = SETTING_RULES[key]
        if typ is str:
            if key == "reset_layout" and raw not in ("password_first", "command_first"):
                raise HTTPException(422, "reset_layout deve ser password_first ou command_first")
            if key == "display_timezone":
                if raw not in available_timezones():
                    raise HTTPException(422, f"display_timezone: fuso desconhecido '{raw}' (ex.: America/Cuiaba, America/Sao_Paulo)")
                if windows_timezone(raw) is None:
                    raise HTTPException(422, f"display_timezone: '{raw}' não tem equivalente no SQL Server; use um fuso do Brasil "
                                             "(America/Cuiaba, America/Sao_Paulo, America/Manaus, ...) ou UTC")
            value = str(raw).strip()
        else:
            try:
                num = typ(float(raw))
            except (TypeError, ValueError):
                raise HTTPException(422, f"{key}: valor inválido '{raw}'") from None
            if (lo is not None and num < lo) or (hi is not None and num > hi):
                raise HTTPException(422, f"{key}: deve estar entre {lo} e {hi}")
            value = str(num)
        await _set_setting(session, key, value)
        if key == "display_timezone":
            # as views do Power BI (SQL Server) só conhecem os nomes de fuso do Windows
            await _set_setting(session, "display_timezone_windows", windows_timezone(value))
    await session.commit()
    return await get_settings_map(session)


async def _set_setting(session: AsyncSession, key: str, value: str) -> None:
    stmt = update(AppSetting).where(AppSetting.key == key).values(value=value, updated_at=datetime.now(UTC))
    if (await session.execute(stmt)).rowcount == 0:
        session.add(AppSetting(key=key, value=value))
        await session.flush()


# ----------------------------------------------------------------------------- comandos
async def _gateway_client_for(session: AsyncSession, meter: Meter, poller: Poller | None) -> tuple[GatewayClient, bool]:
    """Cliente do pool (se o coletor estiver rodando) ou temporário. Retorna (cliente, temporário?)."""
    if poller is not None:
        gw = poller.gateway_client(meter.gateway_id)
        if gw is not None:
            return gw, False
    g = await session.get(Gateway, meter.gateway_id)
    if g is None:
        raise HTTPException(404, "gateway do medidor não encontrado")
    s = get_settings()
    return GatewayClient(g.id, g.name, g.host, g.port, timeout=max(s.modbus_timeout_s, 1.0), retries=1), True


async def run_meter_command(session: AsyncSession, meter_id: int, command: str, requested_by: str, poller: Poller | None) -> CommandLog:
    if command not in COMMANDS:
        raise HTTPException(422, f"comando desconhecido: {command}")
    meter = await get_meter_or_404(session, meter_id)
    cfg = await get_settings_map(session)
    code_key, default_code = COMMANDS[command]
    code = int(float(cfg.get(code_key, default_code)))
    password = int(float(cfg.get("reset_password", 0)))
    layout = cfg.get("reset_layout", "password_first")

    gw, temporary = await _gateway_client_for(session, meter, poller)
    try:
        async with gw.lock:
            result: CommandResult = await send_command(gw, meter.unit_id, code, password=password, layout=layout)
    finally:
        if temporary:
            gw.close()
    entry = CommandLog(
        meter_id=meter.id, command=command, requested_by=requested_by,
        success=result.success, result_code=result.result_code, detail=f"código {code}: {result.detail}",
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    (log.info if result.success else log.warning)("comando %s no medidor %s por %s: %s", command, meter.name, requested_by, entry.detail)
    return entry


async def identify_meter(session: AsyncSession, meter_id: int, poller: Poller | None) -> Meter:
    meter = await get_meter_or_404(session, meter_id)
    gw, temporary = await _gateway_client_for(session, meter, poller)
    try:
        async with gw.lock:
            ident = await read_identification(gw, meter.unit_id)
    finally:
        if temporary:
            gw.close()
    meter.serial_number = ident["serial_number"] or meter.serial_number
    meter.firmware = ident["firmware"] or meter.firmware
    meter.ct_primary = ident["ct_primary"] or meter.ct_primary
    await session.commit()
    await session.refresh(meter)
    return meter


async def list_commands(session: AsyncSession, limit: int = 100) -> list[CommandLog]:
    return list((await session.execute(select(CommandLog).order_by(CommandLog.ts.desc()).limit(limit))).scalars())


# ----------------------------------------------------------------------------- histórico (gráficos)
MAX_HISTORY_METERS = 12         # séries simultâneas no gráfico por grandeza (cobre o maior gateway inteiro)
MAX_HISTORY_POINTS = 50_000     # pontos por série quando o bucket é forçado (30 dias minuto a minuto = 43 200)
MAX_HISTORY_SPAN = timedelta(days=400)

HISTORY_RANGES: dict[str, timedelta] = {
    "15m": timedelta(minutes=15), "1h": timedelta(hours=1), "6h": timedelta(hours=6),
    "24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30),
}
BUCKET_SECONDS: dict[str, int] = {"1s": 1, "5s": 5, "30s": 30, "1min": 60, "5min": 300, "15min": 900, "1h": 3600, "1d": 86400}
BUCKET_LABELS: dict[str, str] = {"1s": "1 segundo", "5s": "5 segundos", "30s": "30 segundos", "1min": "1 minuto", "5min": "5 minutos",
                                 "15min": "15 minutos", "1h": "1 hora", "1d": "1 dia"}
_BUCKET_BY_SECONDS = {v: k for k, v in BUCKET_SECONDS.items()}


# ----------------------------------------------------------------------------- catálogo de grandezas
@dataclass(frozen=True)
class Metric:
    """Uma grandeza que o gráfico por grandeza pode mostrar.

    source: "fast" (reading_fast / agg_fast_1min), "slow" (reading_slow / agg_slow_1h) ou "energy"
    (consumo = diferença do acumulador kwh_import entre buckets). agg é o agregador por bucket.
    """

    key: str
    label: str
    unit: str
    group: str
    source: str
    digits: int = 1
    agg: str = "avg"
    from_zero: bool = False
    range: tuple[float, float] | None = None


def _fast(key, label, unit, group, **kw) -> Metric:
    return Metric(key, label, unit, group, "fast", **kw)


def _slow(key, label, unit, group, **kw) -> Metric:
    return Metric(key, label, unit, group, "slow", **kw)


METRICS: dict[str, Metric] = {m.key: m for m in [
    Metric("kwh", "Consumo de energia no período", "kWh", "Energia", "energy", digits=2, from_zero=True),
    _slow("kwh_import", "Energia ativa importada (acumulada)", "kWh", "Energia", from_zero=True),
    _slow("kwh_export", "Energia ativa exportada (acumulada)", "kWh", "Energia", from_zero=True),
    _slow("kwh_net", "Energia ativa líquida (acumulada)", "kWh", "Energia"),
    _slow("kvarh", "Energia reativa (acumulada)", "kvarh", "Energia", from_zero=True),
    _slow("kvah", "Energia aparente (acumulada)", "kVAh", "Energia", from_zero=True),
    _fast("p_total", "Potência ativa total", "kW", "Potência", digits=2, from_zero=True),
    _fast("p_a", "Potência ativa fase A", "kW", "Potência", digits=2, from_zero=True),
    _fast("p_b", "Potência ativa fase B", "kW", "Potência", digits=2, from_zero=True),
    _fast("p_c", "Potência ativa fase C", "kW", "Potência", digits=2, from_zero=True),
    _fast("q_total", "Potência reativa total", "kvar", "Potência", digits=2),
    _fast("s_total", "Potência aparente total", "kVA", "Potência", digits=2, from_zero=True),
    _fast("pf_total", "Fator de potência total", "", "Fator de potência", digits=3, range=(0, 1)),
    _fast("pf_disp", "Fator de potência de deslocamento", "", "Fator de potência", digits=3, range=(0, 1)),
    _fast("i_avg", "Corrente média das fases", "A", "Corrente", from_zero=True),
    _fast("ia", "Corrente fase A", "A", "Corrente", from_zero=True),
    _fast("ib", "Corrente fase B", "A", "Corrente", from_zero=True),
    _fast("ic", "Corrente fase C", "A", "Corrente", from_zero=True),
    _fast("in_", "Corrente de neutro", "A", "Corrente", from_zero=True),
    _fast("vln_avg", "Tensão L-N média", "V", "Tensão L-N"),
    _fast("van", "Tensão A-N", "V", "Tensão L-N"),
    _fast("vbn", "Tensão B-N", "V", "Tensão L-N"),
    _fast("vcn", "Tensão C-N", "V", "Tensão L-N"),
    _fast("vll_avg", "Tensão L-L média", "V", "Tensão L-L"),
    _fast("vab", "Tensão A-B", "V", "Tensão L-L"),
    _fast("vbc", "Tensão B-C", "V", "Tensão L-L"),
    _fast("vca", "Tensão C-A", "V", "Tensão L-L"),
    _slow("demand_present", "Demanda atual", "kW", "Demanda", digits=2, from_zero=True),
    _slow("demand_last", "Demanda do último intervalo", "kW", "Demanda", digits=2, from_zero=True),
    _slow("demand_peak", "Demanda de pico", "kW", "Demanda", digits=2, agg="max", from_zero=True),
    _slow("freq", "Frequência", "Hz", "Frequência", digits=2),
    _slow("thd_ia", "THD corrente fase A", "%", "THD", from_zero=True),
    _slow("thd_ib", "THD corrente fase B", "%", "THD", from_zero=True),
    _slow("thd_ic", "THD corrente fase C", "%", "THD", from_zero=True),
    _slow("thd_van", "THD tensão A-N", "%", "THD", from_zero=True),
    _slow("thd_vbn", "THD tensão B-N", "%", "THD", from_zero=True),
    _slow("thd_vcn", "THD tensão C-N", "%", "THD", from_zero=True),
]}
METRIC_GROUPS: list[str] = list(dict.fromkeys(m.group for m in METRICS.values()))
# Resolução mínima por tipo de grandeza: o ciclo lento amostra a cada 30 s (bucket mínimo 1 min); o consumo é a
# diferença do acumulador entre buckets e abaixo de 5 min vira ruído.
MIN_STEP_S: dict[str, int] = {"fast": 1, "slow": 60, "energy": 300}


def metric_catalog() -> list[dict]:
    """Catálogo para a página de gráficos (select agrupado)."""
    # min_step_s: menor resolução que faz sentido (a tela esconde as opções abaixo dela; plan_for_metric impõe)
    return [
        {"key": m.key, "label": m.label, "unit": m.unit, "group": m.group, "digits": m.digits,
         "from_zero": m.from_zero, "range": list(m.range) if m.range else None, "min_step_s": MIN_STEP_S[m.source]}
        for m in METRICS.values()
    ]


def get_metric(key: str) -> Metric:
    m = METRICS.get(key)
    if m is None:
        raise HTTPException(422, f"grandeza desconhecida: '{key}'")
    return m


# ----------------------------------------------------------------------------- resolução (bucket)
@dataclass(frozen=True)
class BucketPlan:
    """Resolução automática por período: bucket das grandezas rápidas e bucket do consumo de energia."""

    label: str
    step_s: int
    source: str
    energy_label: str
    energy_step_s: int
    energy_source: str


def choose_bucket(span: timedelta) -> BucketPlan:
    """Resolução automática: ~1 500 pontos por série; janelas curtas vão à hypertable, longas aos agregados."""
    s = span.total_seconds()
    if s <= 15 * 60:
        return BucketPlan("1s", 1, "reading_fast", "5min", 300, "reading_slow")
    if s <= 3600:
        return BucketPlan("5s", 5, "reading_fast", "5min", 300, "reading_slow")
    if s <= 6 * 3600:
        return BucketPlan("30s", 30, "reading_fast", "15min", 900, "reading_slow")
    if s <= 24 * 3600:
        return BucketPlan("1min", 60, "agg_fast_1min", "15min", 900, "reading_slow")
    if s <= 7 * 86400:
        return BucketPlan("15min", 900, "agg_fast_1min", "1h", 3600, "agg_energy_1h")
    if s <= 90 * 86400:
        return BucketPlan("1h", 3600, "agg_fast_1min", "1d", 86400, "agg_energy_1d")
    return BucketPlan("1d", 86400, "agg_fast_1min", "1d", 86400, "agg_energy_1d")


@dataclass(frozen=True)
class MetricPlan:
    """Bucket efetivo e fonte de uma grandeza específica."""

    label: str
    step_s: int
    source: str


def _source_for(metric: Metric, step_s: int) -> str:
    if metric.source == "fast":
        return "reading_fast" if step_s < 60 else "agg_fast_1min"
    if metric.source == "slow":
        return "reading_slow" if step_s < 3600 else "agg_slow_1h"
    if step_s >= 86400:
        return "agg_energy_1d"
    return "agg_energy_1h" if step_s >= 3600 else "reading_slow"


def plan_for_metric(metric: Metric, span: timedelta, bucket: str = "auto") -> MetricPlan:
    """Resolução de uma grandeza: automática pelo período ou forçada pelo cliente (limitada em pontos).

    Grandezas do ciclo lento são amostradas a cada 30 s: o bucket mínimo é 1 min. O consumo de energia usa no
    mínimo 5 min (a diferença do acumulador em buckets menores vira ruído).
    """
    auto = choose_bucket(span)
    if bucket == "auto":
        step = auto.energy_step_s if metric.source == "energy" else auto.step_s
    else:
        if bucket not in BUCKET_SECONDS:
            raise HTTPException(422, f"bucket deve ser um de {', '.join(BUCKET_SECONDS)} ou auto")
        step = BUCKET_SECONDS[bucket]
        if span.total_seconds() / step > MAX_HISTORY_POINTS:
            raise HTTPException(422, f"intervalo grande demais para a resolução {bucket} (máx. {MAX_HISTORY_POINTS} pontos por série); escolha uma resolução maior")
    step = max(step, MIN_STEP_S[metric.source])
    return MetricPlan(_BUCKET_BY_SECONDS[step], step, _source_for(metric, step))


# ----------------------------------------------------------------------------- parâmetros da API
def parse_meter_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            raise HTTPException(422, f"meter_ids inválido: '{part}'") from None
        if value not in ids:
            ids.append(value)
    if not ids:
        raise HTTPException(422, "informe ao menos um medidor em meter_ids")
    if len(ids) > MAX_HISTORY_METERS:
        raise HTTPException(422, f"no máximo {MAX_HISTORY_METERS} medidores por consulta")
    return ids


def _parse_ts(raw: str, name: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, f"{name}: data inválida '{raw}' (use ISO 8601)") from None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def parse_history_window(range_: str | None, frm: str | None, to: str | None, now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(UTC)
    if range_:
        if range_ not in HISTORY_RANGES:
            raise HTTPException(422, f"range deve ser um de {', '.join(HISTORY_RANGES)}")
        return now - HISTORY_RANGES[range_], now
    if not frm:
        raise HTTPException(422, "informe range ou from")
    start = _parse_ts(frm, "from")
    end = _parse_ts(to, "to") if to else now
    if start >= end:
        raise HTTPException(422, "from deve ser anterior a to")
    if end - start > MAX_HISTORY_SPAN:
        raise HTTPException(422, f"período máximo de {MAX_HISTORY_SPAN.days} dias")
    return start, end


# ----------------------------------------------------------------------------- SQL (T-SQL)
# Buckets em epoch (segundos UTC): DATEDIFF_BIG(second, '1970-01-01', ts) / step * step, devolvidos como inteiros.
# Os agregados agg_* são tabelas de resumo preenchidas a cada minuto (app/db/rollup.py): antes do watermark de
# agg_state lemos a tabela de resumo; a partir dele, as tabelas brutas (o bucket em andamento sai sempre do bruto).
# Parâmetros de tempo entram como UtcDateTime (DATETIME2 sem fuso, em UTC).
_EPOCH = "DATEDIFF_BIG(second, '1970-01-01', {col})"
_WM = "(SELECT COALESCE((SELECT watermark FROM agg_state WHERE name = '{agg}'), CAST('1900-01-01' AS DATETIME2)))"
_AGG_BASE_S = {"agg_fast_1min": 60, "agg_slow_1h": 3600, "agg_energy_1h": 3600, "agg_energy_1d": 86400}
_AGG_RAW = {"agg_fast_1min": "reading_fast", "agg_slow_1h": "reading_slow", "agg_energy_1h": "reading_slow", "agg_energy_1d": "reading_slow"}


def _eb(col: str, step: str | int) -> str:
    """Início do bucket (epoch s) de `step` segundos que contém a coluna de tempo."""
    return f"({_EPOCH.format(col=col)} / {step}) * {step}"


def _text(stmt: str):
    """text() com os tipos dos parâmetros de tempo e a lista expansível de medidores."""
    binds = [bindparam("ids", expanding=True)]
    binds += [bindparam(name, type_=UtcDateTime) for name in ("frm", "to", "efrm") if re.search(rf":{name}\b", stmt)]
    return text(stmt).bindparams(*binds)


# Consumo = diferença do acumulador entre buckets consecutivos. O bucket anterior a :frm (:efrm) entra só para
# alimentar o lag; o chamador descarta buckets < :frm. Se o acumulador caiu (reset de energia), o consumo é o
# valor pós-reset. "last" por bucket = ROW_NUMBER() ... ORDER BY ts DESC (o T-SQL não tem last(valor, tempo)).
def _energy_cte(source: str) -> str:
    if source == "reading_slow":
        return f"""
WITH b AS (
  SELECT meter_id, bucket, kwh_last FROM (
    SELECT meter_id, {_eb('ts', ':step')} AS bucket, kwh_import AS kwh_last,
           ROW_NUMBER() OVER (PARTITION BY meter_id, {_eb('ts', ':step')} ORDER BY ts DESC) AS rn
    FROM reading_slow WHERE meter_id IN :ids AND ts >= :efrm AND ts < :to AND kwh_import IS NOT NULL) x
  WHERE rn = 1)"""
    base, wm = _AGG_BASE_S[source], _WM.format(agg=source)
    return f"""
WITH u AS (
  SELECT meter_id, {_EPOCH.format(col='bucket')} AS b, kwh_last FROM {source}
  WHERE meter_id IN :ids AND bucket >= :efrm AND bucket < :to AND bucket < {wm} AND kwh_last IS NOT NULL
  UNION ALL
  SELECT meter_id, b, kwh_last FROM (
    SELECT meter_id, {_eb('ts', base)} AS b, kwh_import AS kwh_last,
           ROW_NUMBER() OVER (PARTITION BY meter_id, {_eb('ts', base)} ORDER BY ts DESC) AS rn
    FROM reading_slow WHERE meter_id IN :ids AND ts >= :efrm AND ts < :to AND ts >= {wm} AND kwh_import IS NOT NULL) r
  WHERE rn = 1),
b AS (
  SELECT meter_id, bucket, kwh_last FROM (
    SELECT meter_id, (b / :step) * :step AS bucket, kwh_last,
           ROW_NUMBER() OVER (PARTITION BY meter_id, (b / :step) * :step ORDER BY b DESC) AS rn FROM u) x
  WHERE rn = 1)"""


_ENERGY_TAIL = """
d AS (
  SELECT meter_id, bucket, kwh_last,
         CASE WHEN kwh_last - lag(kwh_last) OVER (PARTITION BY meter_id ORDER BY bucket) < 0 THEN kwh_last
              ELSE kwh_last - lag(kwh_last) OVER (PARTITION BY meter_id ORDER BY bucket) END AS v
  FROM b)"""
_RANKING_SQL = {
    source: _text(_energy_cte(source) + "," + _ENERGY_TAIL + "\nSELECT meter_id, sum(v) AS kwh FROM d WHERE bucket >= :frm_s GROUP BY meter_id")
    for source in ("reading_slow", "agg_energy_1h", "agg_energy_1d")
}
# Nome de coluna e agregador vêm de METRICS (nunca do request); entram na f-string só depois de validados.
_sql_cache: dict[tuple[str, str], object] = {}


def _metric_sql(metric: Metric, source: str):
    key = (metric.key, source)
    if key in _sql_cache:
        return _sql_cache[key]
    if metric.source == "energy":
        stmt = _energy_cte(source) + "," + _ENERGY_TAIL + "\nSELECT meter_id, bucket, kwh_last, v FROM d ORDER BY 1, 2"
    elif source in ("reading_fast", "reading_slow"):
        stmt = f"""
SELECT meter_id, bucket, {metric.agg}(v) AS v FROM (
  SELECT meter_id, {_eb('ts', ':step')} AS bucket, {metric.key} AS v FROM {source}
  WHERE meter_id IN :ids AND ts >= :frm AND ts < :to) x
GROUP BY meter_id, bucket ORDER BY 1, 2"""
    else:
        # rollup sobre a tabela de resumo (média de médias; max usa a coluna *_max) + cauda bruta a partir do
        # watermark, já reduzida ao bucket base do agregado para pesar o mesmo que uma linha materializada
        col = f"{metric.key}_max" if metric.agg == "max" else metric.key
        base, wm, raw = _AGG_BASE_S[source], _WM.format(agg=source), _AGG_RAW[source]
        stmt = f"""
SELECT meter_id, bucket, {metric.agg}(v) AS v FROM (
  SELECT meter_id, (b / :step) * :step AS bucket, v FROM (
    SELECT meter_id, {_EPOCH.format(col='bucket')} AS b, {col} AS v FROM {source}
    WHERE meter_id IN :ids AND bucket >= :frm AND bucket < :to AND bucket < {wm}
    UNION ALL
    SELECT meter_id, b, {metric.agg}(v) AS v FROM (
      SELECT meter_id, {_eb('ts', base)} AS b, {metric.key} AS v FROM {raw}
      WHERE meter_id IN :ids AND ts >= :frm AND ts < :to AND ts >= {wm}) r
    GROUP BY meter_id, b) u) w
GROUP BY meter_id, bucket ORDER BY 1, 2"""
    _sql_cache[key] = _text(stmt)
    return _sql_cache[key]


def _epoch_s(value) -> int:
    return int(value) if isinstance(value, (int, float)) else int(value.timestamp())


def _align(rows, meter_ids: list[int], frm: datetime | None = None, key: str = "v") -> tuple[list[int], dict[int, list]]:
    """Alinha linhas (meter_id, bucket, v) num único eixo de tempo (epoch s); None onde o medidor não tem amostra."""
    frm_s = _epoch_s(frm) if frm is not None else None
    rows = [r for r in rows if frm_s is None or _epoch_s(r["bucket"]) >= frm_s]
    buckets = sorted({_epoch_s(r["bucket"]) for r in rows})
    index = {b: i for i, b in enumerate(buckets)}
    series = {mid: [None] * len(buckets) for mid in meter_ids}
    for r in rows:
        target = series.get(r["meter_id"])
        if target is None:
            continue
        v = r[key]
        target[index[_epoch_s(r["bucket"])]] = float(v) if v is not None else None
    return buckets, series


def _history_params(meter_ids: list[int], frm: datetime, to: datetime, step_s: int) -> dict:
    # efrm: um bucket antes do início, só para alimentar o lag() do primeiro bucket visível do consumo
    return {"ids": meter_ids, "frm": frm, "to": to, "step": int(step_s), "efrm": frm - timedelta(seconds=step_s),
            "frm_s": int(frm.timestamp())}


async def meter_history(session: AsyncSession, metric: Metric, meter_ids: list[int], frm: datetime, to: datetime, plan: MetricPlan) -> dict:
    """Série temporal de uma grandeza para até MAX_HISTORY_METERS medidores, alinhada num eixo único (epoch s UTC)."""
    meters = {m.id: m for m in await list_meters(session) if m.id in meter_ids}
    missing = [i for i in meter_ids if i not in meters]
    if missing:
        raise HTTPException(404, f"medidor não encontrado: {', '.join(map(str, missing))}")
    params = _history_params(meter_ids, frm, to, plan.step_s)
    rows = (await session.execute(_metric_sql(metric, plan.source), params)).mappings().all()
    t, series = _align(rows, meter_ids, frm=frm if metric.source == "energy" else None)
    return {
        "metric": metric.key, "label": metric.label, "unit": metric.unit, "digits": metric.digits,
        "from_zero": metric.from_zero, "range": list(metric.range) if metric.range else None,
        "bucket": plan.label, "step_s": plan.step_s, "source": plan.source,
        "from": frm.isoformat(), "to": to.isoformat(), "t": t,
        "series": {str(mid): {"name": meters[mid].name, "gateway_id": meters[mid].gateway_id, "v": series[mid]} for mid in meter_ids},
    }


def ranking_plan(span: timedelta) -> MetricPlan:
    """Buckets do ranking: pequenos o bastante para o erro nas bordas do período ser desprezível."""
    s = span.total_seconds()
    if s <= 7 * 86400:
        return MetricPlan("5min", 300, "reading_slow")
    if s <= 90 * 86400:
        return MetricPlan("1h", 3600, "agg_energy_1h")
    return MetricPlan("1d", 86400, "agg_energy_1d")


async def energy_ranking(session: AsyncSession, frm: datetime, to: datetime) -> dict:
    """kWh consumido no período por todos os medidores, em ordem decrescente, com o total por gateway."""
    gateways = await list_gateways(session)
    meters = await list_meters(session)
    plan = ranking_plan(to - frm)
    kwh: dict[int, float] = {}
    if meters:
        params = _history_params([m.id for m in meters], frm, to, plan.step_s)
        rows = (await session.execute(_RANKING_SQL[plan.source], params)).mappings().all()
        kwh = {r["meter_id"]: float(r["kwh"] or 0.0) for r in rows}
    out_meters = [
        {"meter_id": m.id, "name": m.name, "gateway_id": m.gateway_id, "kwh": round(kwh.get(m.id, 0.0), 3)}
        for m in meters if m.enabled or kwh.get(m.id, 0.0) > 0
    ]
    out_meters.sort(key=lambda r: (-r["kwh"], r["name"].lower()))
    by_gw = {g.id: {"gateway_id": g.id, "name": g.name, "color": g.color, "kwh": 0.0, "meters": 0} for g in gateways}
    for r in out_meters:
        g = by_gw.get(r["gateway_id"])
        if g:
            g["kwh"] += r["kwh"]
            g["meters"] += 1
    for g in by_gw.values():
        g["kwh"] = round(g["kwh"], 3)
    return {
        "from": frm.isoformat(), "to": to.isoformat(), "bucket": plan.label, "source": plan.source,
        "total_kwh": round(sum(r["kwh"] for r in out_meters), 3),
        "gateways": list(by_gw.values()), "meters": out_meters,
    }
