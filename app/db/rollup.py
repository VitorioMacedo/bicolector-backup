"""Agregados de resumo (substituem os agregados contínuos do TimescaleDB) e retenção das tabelas brutas.

Cada tabela agg_* é recalculada a cada minuto para a janela [watermark - 1 bucket, agora + 1 bucket): o bucket
anterior é refeito para absorver leituras que chegaram atrasadas, e o bucket em andamento é gravado parcial
(o Power BI enxerga o dia/hora corrente). agg_state.watermark guarda o início do bucket em andamento: as
consultas do painel usam a tabela de resumo antes dele e as tabelas brutas a partir dele (app/services.py).

Fontes: agg_1min, agg_fast_1min <- reading_fast; agg_15min, agg_1h <- agg_1min; agg_1d <- agg_1h;
agg_energy_1h, agg_energy_1d, agg_slow_1h <- reading_slow.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import FAST_COLUMNS, SLOW_COLUMNS

log = logging.getLogger(__name__)

EPOCH = datetime(2000, 1, 1, tzinfo=UTC)   # base dos buckets (a mesma do SQL)
ROLLUP_INTERVAL_S = 60.0
RETENTION_INTERVAL_S = 600.0
RETENTION_BATCH = 20_000
POLL_CYCLE_RETENTION_DAYS = 30


def bucket_sql(ts_col: str, minutes: int) -> str:
    """Início do bucket de `minutes` minutos que contém ts_col (DATETIME2 em UTC)."""
    return f"DATEADD(minute, (DATEDIFF(minute, '2000-01-01', {ts_col}) / {minutes}) * {minutes}, '2000-01-01')"


def floor_bucket(ts: datetime, minutes: int) -> datetime:
    step = timedelta(minutes=minutes)
    return EPOCH + ((ts - EPOCH) // step) * step


@dataclass(frozen=True)
class AggDef:
    name: str
    source: str
    ts_col: str          # coluna de tempo da fonte: ts (bruta) ou bucket (agregado)
    minutes: int
    columns: str         # lista de colunas destino (depois de bucket, meter_id)
    select: str          # expressões correspondentes (podem usar {B} = expressão do bucket)
    inner: str = ""      # subconsulta opcional (energia: FIRST_VALUE por bucket); usa {B}, :lo, :hi
    chunk: timedelta = timedelta(days=1)

    def insert_sql(self) -> str:
        b = bucket_sql(self.ts_col, self.minutes)
        if self.inner:
            src = "(" + self.inner.format(B=b) + ") x"
            group = "bucket"
        else:
            src = f"{self.source} WHERE {self.ts_col} >= :lo AND {self.ts_col} < :hi"
            group = b
        return (f"INSERT INTO {self.name} (bucket, meter_id, {self.columns})\n"
                f"SELECT {group} AS bucket, meter_id, {self.select.format(B=b)}\nFROM {src}\nGROUP BY {group}, meter_id")


def _avg(cols: list[str]) -> str:
    return ", ".join(f"AVG({c}) AS {c}" for c in cols)


_PBI_FAST_SELECT = ("AVG(p_total), MIN(p_total), MAX(p_total), AVG(q_total), AVG(s_total), AVG(i_avg), MAX(i_avg), "
                    "AVG(ia), AVG(ib), AVG(ic), AVG(vln_avg), MIN(vln_avg), MAX(vln_avg), AVG(vll_avg), AVG(pf_total), MIN(pf_total), COUNT(*)")
_PBI_FAST_COLUMNS = ("p_avg, p_min, p_max, q_avg, s_avg, i_avg, i_max, ia_avg, ib_avg, ic_avg, vln_avg, vln_min, vln_max, vll_avg, "
                     "pf_avg, pf_min, samples")
_PBI_ROLL_COLUMNS = "p_avg, p_min, p_max, q_avg, s_avg, i_avg, i_max, vln_avg, vln_min, vln_max, pf_avg, pf_min, samples"
_PBI_ROLL_SELECT = ("AVG(p_avg), MIN(p_min), MAX(p_max), AVG(q_avg), AVG(s_avg), AVG(i_avg), MAX(i_max), "
                    "AVG(vln_avg), MIN(vln_min), MAX(vln_max), AVG(pf_avg), MIN(pf_min), SUM(samples)")
_THD = ["thd_ia", "thd_ib", "thd_ic", "thd_van", "thd_vbn", "thd_vcn"]
# primeiro/último acumulador do bucket ignorando nulos (FIRST_VALUE ... IGNORE NULLS só existe no SQL Server 2022)
_FIRST = "FIRST_VALUE({c}) OVER (PARTITION BY meter_id, {{B}} ORDER BY CASE WHEN {c} IS NULL THEN 1 ELSE 0 END, ts) AS {c}_first"
_LAST = "FIRST_VALUE({c}) OVER (PARTITION BY meter_id, {{B}} ORDER BY CASE WHEN {c} IS NULL THEN 1 ELSE 0 END, ts DESC) AS {c}_last"
_ENERGY_INNER = ("SELECT {B} AS bucket, meter_id, demand_peak, freq, " + ", ".join(_THD) + ", "
                 + ", ".join(t.format(c=c) for c in ("kwh_import", "kwh_export", "kvarh") for t in (_FIRST, _LAST))
                 + " FROM reading_slow WHERE ts >= :lo AND ts < :hi")

AGGREGATES: list[AggDef] = [
    AggDef("agg_fast_1min", "reading_fast", "ts", 1, ", ".join(FAST_COLUMNS) + ", samples", _avg(FAST_COLUMNS) + ", COUNT(*)"),
    AggDef("agg_1min", "reading_fast", "ts", 1, _PBI_FAST_COLUMNS, _PBI_FAST_SELECT),
    AggDef("agg_15min", "agg_1min", "bucket", 15, _PBI_ROLL_COLUMNS, _PBI_ROLL_SELECT, chunk=timedelta(days=30)),
    AggDef("agg_1h", "agg_1min", "bucket", 60, _PBI_ROLL_COLUMNS, _PBI_ROLL_SELECT, chunk=timedelta(days=30)),
    AggDef("agg_1d", "agg_1h", "bucket", 1440, "p_avg, p_min, p_max, i_avg, i_max, vln_avg, vln_min, vln_max, pf_avg, pf_min, samples",
           "AVG(p_avg), MIN(p_min), MAX(p_max), AVG(i_avg), MAX(i_max), AVG(vln_avg), MIN(vln_min), MAX(vln_max), AVG(pf_avg), MIN(pf_min), SUM(samples)",
           chunk=timedelta(days=365)),
    AggDef("agg_energy_1h", "reading_slow", "ts", 60,
           "kwh_first, kwh_last, kwh_consumed, kwh_exported, kvarh_consumed, demand_peak, freq_avg, " + ", ".join(_THD),
           "MIN(kwh_import_first), MIN(kwh_import_last), MIN(kwh_import_last) - MIN(kwh_import_first), "
           "MIN(kwh_export_last) - MIN(kwh_export_first), MIN(kvarh_last) - MIN(kvarh_first), MAX(demand_peak), AVG(freq), "
           + ", ".join(f"AVG({c})" for c in _THD), inner=_ENERGY_INNER, chunk=timedelta(days=7)),
    AggDef("agg_energy_1d", "reading_slow", "ts", 1440, "kwh_first, kwh_last, kwh_consumed, kwh_exported, demand_peak",
           "MIN(kwh_import_first), MIN(kwh_import_last), MIN(kwh_import_last) - MIN(kwh_import_first), "
           "MIN(kwh_export_last) - MIN(kwh_export_first), MAX(demand_peak)", inner=_ENERGY_INNER, chunk=timedelta(days=7)),
    AggDef("agg_slow_1h", "reading_slow", "ts", 60, ", ".join(SLOW_COLUMNS) + ", demand_peak_max, samples",
           _avg(SLOW_COLUMNS) + ", MAX(demand_peak), COUNT(*)", chunk=timedelta(days=7)),
]
AGG_BY_NAME = {a.name: a for a in AGGREGATES}


def _naive(ts: datetime) -> datetime:
    return ts.astimezone(UTC).replace(tzinfo=None)


async def refresh_aggregate(engine: AsyncEngine, agg: AggDef, now: datetime | None = None) -> int:
    """Recalcula a janela pendente de uma tabela de resumo; devolve quantos buckets (chunks) foram refeitos."""
    now = now or datetime.now(UTC)
    step = timedelta(minutes=agg.minutes)
    current = floor_bucket(now, agg.minutes)
    async with engine.connect() as conn:
        wm = (await conn.execute(text("SELECT watermark FROM agg_state WHERE name = :n"), {"n": agg.name})).scalar()
        if wm is None:
            first = (await conn.execute(text(f"SELECT MIN({agg.ts_col}) FROM {agg.source}"))).scalar()
            if first is None:
                return 0
            lo = floor_bucket(first.replace(tzinfo=UTC), agg.minutes)
        else:
            lo = wm.replace(tzinfo=UTC) - step
    chunks = await refresh_window(engine, agg, lo, current + step)
    async with engine.begin() as conn:
        await conn.execute(text("""
MERGE agg_state AS t USING (SELECT :n AS name) AS s ON t.name = s.name
WHEN MATCHED THEN UPDATE SET watermark = :wm, updated_at = :now
WHEN NOT MATCHED THEN INSERT (name, watermark, updated_at) VALUES (:n, :wm, :now);"""),
                           {"n": agg.name, "wm": _naive(current), "now": _naive(now)})
    return chunks


async def refresh_window(engine: AsyncEngine, agg: AggDef, lo: datetime, hi: datetime) -> int:
    """Recalcula os buckets de [lo, hi) em pedaços (apaga e reinsere); não mexe no watermark."""
    insert_sql = text(agg.insert_sql())
    chunks = 0
    while lo < hi:
        chunk_hi = min(hi, lo + agg.chunk)
        async with engine.begin() as conn:
            await conn.execute(text(f"DELETE FROM {agg.name} WHERE bucket >= :lo AND bucket < :hi"), {"lo": _naive(lo), "hi": _naive(chunk_hi)})
            await conn.execute(insert_sql, {"lo": _naive(lo), "hi": _naive(chunk_hi)})
        chunks += 1
        lo = chunk_hi
    return chunks


async def refresh_all(engine: AsyncEngine, now: datetime | None = None) -> None:
    for agg in AGGREGATES:   # na ordem: os hierárquicos leem os de 1 min recém-atualizados
        await refresh_aggregate(engine, agg, now)


async def purge_old_rows(engine: AsyncEngine, table: str, days: int, now: datetime | None = None) -> int:
    """Apaga em lotes (não trava a tabela) o que é mais velho que `days` dias; devolve o total apagado."""
    cutoff = _naive((now or datetime.now(UTC)) - timedelta(days=days))
    total = 0
    while True:
        async with engine.begin() as conn:
            res = await conn.execute(text(f"DELETE TOP ({RETENTION_BATCH}) FROM {table} WHERE ts < :cut"), {"cut": cutoff})
        total += res.rowcount
        if res.rowcount < RETENTION_BATCH:
            return total
        await asyncio.sleep(0.2)


class Maintenance:
    """Tarefa de fundo: rollup a cada minuto e retenção a cada 10 min (só no SQL Server)."""

    def __init__(self, engine: AsyncEngine, raw_retention_days: Callable[[], int]):
        self.engine = engine
        self.raw_retention_days = raw_retention_days
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.last_rollup_at: datetime | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self.engine.dialect.name != "mssql":
            log.warning("manutenção dos agregados desativada: banco %s", self.engine.dialect.name)
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="db-maintenance")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _loop(self) -> None:
        last_retention = 0.0
        while not self._stop.is_set():
            try:
                await refresh_all(self.engine)
                self.last_rollup_at = datetime.now(UTC)
                self.last_error = None
                loop_time = asyncio.get_running_loop().time()
                if loop_time - last_retention >= RETENTION_INTERVAL_S:
                    last_retention = loop_time
                    n = await purge_old_rows(self.engine, "reading_fast", self.raw_retention_days())
                    n += await purge_old_rows(self.engine, "poll_cycle", POLL_CYCLE_RETENTION_DAYS)
                    if n:
                        log.info("retenção: %d linhas antigas apagadas", n)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                log.error("manutenção do banco falhou: %s", self.last_error)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=ROLLUP_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
