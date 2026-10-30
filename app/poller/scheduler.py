"""Agendador do coletor: uma task por gateway, dois ciclos (rápido e lento) por task.

Ciclo rápido (padrão 1 s): lê um bloco por medidor, todos os medidores do gateway em sequência.
Ciclo lento (padrão 30 s): energia/demanda/THD, lido no tempo que sobra do ciclo rápido para não
estourar o orçamento. Um medidor muito atrasado é lido mesmo sem folga (no máximo um por ciclo).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.modbus import registers as R
from app.modbus.client import GatewayClient, ModbusError
from app.modbus.gateway_pool import GatewayPool
from app.poller.config import GatewayCfg, MeterCfg, PollConfig, load_config
from app.poller.state import GatewayLive, MeterLive, RuntimeState
from app.poller.writer import BatchWriter

log = logging.getLogger(__name__)

SLOW_READ_COST_FACTOR = 3.0  # uma rodada lenta são 3 requisições menores que o bloco rápido; estimada em latências médias
SLOW_READ_MARGIN_S = 0.02
BACKOFF_MIN_S = 5.0
BACKOFF_MAX_S = 60.0


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=(datetime.now(UTC).microsecond // 1000) * 1000)


class GatewayRunner:
    def __init__(self, cfg: GatewayCfg, poller: "Poller"):
        self.cfg = cfg
        self.poller = poller
        self.live: GatewayLive = poller.state.gateways.setdefault(
            cfg.id, GatewayLive(cfg.id, cfg.name, cfg.host, cfg.port, fast_block=cfg.fast_block)
        )
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._slow_cursor = 0
        self._avg_latency_ms = 80.0
        self._started_at = time.monotonic()

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name=f"gateway-{self.cfg.id}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def update_cfg(self, cfg: GatewayCfg) -> None:
        self.cfg = cfg
        self.live.name, self.live.host, self.live.port, self.live.fast_block = cfg.name, cfg.host, cfg.port, cfg.fast_block
        wanted = {m.id for m in cfg.meters}
        for mid in list(self.live.meters):
            if mid not in wanted:
                self.live.meters.pop(mid)
        for m in cfg.meters:
            ml = self.live.meters.get(m.id)
            if ml is None:
                self.live.meters[m.id] = MeterLive(m.id, m.name, m.unit_id, cfg.id)
            else:
                ml.name, ml.unit_id = m.name, m.unit_id

    # ------------------------------------------------------------------
    async def run(self) -> None:
        self.update_cfg(self.cfg)
        while not self._stop.is_set():
            gw = self.poller.pool.get(self.cfg.id)
            pc = self.poller.config
            if gw is None:
                await asyncio.sleep(0.5)
                continue
            interval = pc.fast_interval_s
            t0 = time.perf_counter()
            ts = _now()
            ok = failed = 0
            block = R.FAST_BLOCKS.get(self.cfg.fast_block, R.FAST_FULL)

            for m in self.cfg.meters:
                if self._stop.is_set():
                    return
                ml = self.live.meters[m.id]
                if not ml.online and ml.consecutive_failures >= pc.offline_after_failures and time.monotonic() < ml.next_retry_at:
                    continue  # offline em backoff: não gasta tempo de barramento
                try:
                    async with gw.lock:
                        data = await gw.read_block(m.unit_id, block)
                    self._mark_ok(ml, gw, ts)
                    ml.last_fast, ml.last_fast_at = data, ts
                    self.poller.writer.add_fast({"ts": ts, "meter_id": m.id, **data})
                    ok += 1
                except ModbusError as exc:
                    self._mark_fail(ml, pc, ts, str(exc))
                    failed += 1
            self.live.connected = gw.connected

            # ciclo lento no tempo ocioso
            slow_reads = 0
            while not self._stop.is_set():
                elapsed = time.perf_counter() - t0
                remaining = interval - elapsed
                target, overdue = self._next_slow_meter(pc)
                if target is None:
                    break
                budget_needed = SLOW_READ_COST_FACTOR * self._avg_latency_ms / 1000 + SLOW_READ_MARGIN_S
                if remaining < budget_needed and not (overdue and slow_reads == 0):
                    break
                await self._read_slow(gw, target, pc)
                slow_reads += 1
                if overdue:
                    break

            duration_ms = (time.perf_counter() - t0) * 1000
            self.live.record_cycle(duration_ms, interval * 1000, ts)
            self.poller.writer.add_cycle({
                "ts": ts, "gateway_id": self.cfg.id, "duration_ms": int(duration_ms), "interval_ms": int(interval * 1000),
                "meters_ok": ok, "meters_failed": failed, "slow_reads": slow_reads,
            })
            sleep_for = max(0.0, interval - (time.perf_counter() - t0))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    def _next_slow_meter(self, pc: PollConfig) -> tuple[MeterCfg | None, bool]:
        """Próximo medidor (round-robin) cuja leitura lenta venceu. overdue = atrasado mais de 2x o intervalo."""
        meters = [m for m in self.cfg.meters if self.live.meters[m.id].online or self.live.meters[m.id].consecutive_failures < pc.offline_after_failures]
        if not meters:
            return None, False
        now = datetime.now(UTC)
        since_start = time.monotonic() - self._started_at
        n = len(meters)
        for i in range(n):
            m = meters[(self._slow_cursor + i) % n]
            ml = self.live.meters[m.id]
            # medidor nunca lido conta a partir do início do runner, para não forçar 1 leitura por ciclo no arranque
            age = (now - ml.last_slow_at).total_seconds() if ml.last_slow_at else since_start
            if age >= pc.slow_interval_s or ml.last_slow_at is None:
                self._slow_cursor = (self._slow_cursor + i + 1) % n
                return m, age >= 2 * pc.slow_interval_s
        return None, False

    async def _read_slow(self, gw: GatewayClient, m: MeterCfg, pc: PollConfig) -> None:
        ml = self.live.meters[m.id]
        ts = _now()
        row: dict = {"ts": ts, "meter_id": m.id}
        try:
            for block in R.SLOW_BLOCKS:
                async with gw.lock:
                    row.update(await gw.read_block(m.unit_id, block))
        except ModbusError as exc:
            self._mark_fail(ml, pc, ts, f"ciclo lento: {exc}")
            ml.last_slow_at = ts  # evita insistir no mesmo medidor a cada ciclo
            return
        self._mark_ok(ml, gw, ts)
        ml.last_slow, ml.last_slow_at = {k: v for k, v in row.items() if k not in ("ts", "meter_id")}, ts
        self.poller.writer.add_slow(row)

    def _mark_ok(self, ml: MeterLive, gw: GatewayClient, ts: datetime) -> None:
        self._avg_latency_ms = 0.9 * self._avg_latency_ms + 0.1 * gw.last_latency_ms
        ml.last_latency_ms = gw.last_latency_ms
        changed = (not ml.online) or ml.consecutive_failures
        ml.online, ml.consecutive_failures, ml.last_ok_at = True, 0, ts
        if changed:
            log.info("medidor %s (unit %s) online", ml.name, ml.unit_id)
            self.poller.writer.set_status(ml.meter_id, last_ok_at=ts, consecutive_failures=0)
        elif ml.last_ok_at and (ts.second % 30 == 0):
            self.poller.writer.set_status(ml.meter_id, last_ok_at=ts, consecutive_failures=0)

    def _mark_fail(self, ml: MeterLive, pc: PollConfig, ts: datetime, error: str) -> None:
        ml.consecutive_failures += 1
        ml.last_error, ml.last_error_at = error, ts
        if ml.consecutive_failures >= pc.offline_after_failures:
            if ml.online or ml.consecutive_failures == pc.offline_after_failures:
                log.warning("medidor %s (unit %s) offline: %s", ml.name, ml.unit_id, error)
            ml.online = False
            backoff = min(BACKOFF_MAX_S, BACKOFF_MIN_S * 2 ** (ml.consecutive_failures - pc.offline_after_failures))
            ml.next_retry_at = time.monotonic() + backoff
        self.poller.writer.set_status(
            ml.meter_id, last_error=error[:500], last_error_at=ts, consecutive_failures=ml.consecutive_failures
        )


class Poller:
    def __init__(self, engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession], timeout: float, retries: int, config_refresh_s: float = 2.0):
        self.engine = engine
        self.session_factory = session_factory
        self.state = RuntimeState()
        self.pool = GatewayPool(timeout=timeout, retries=retries)
        self.writer = BatchWriter(engine)
        self.config = PollConfig()
        self.config_refresh_s = config_refresh_s
        self._runners: dict[int, GatewayRunner] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        await self.reload()
        self.writer.start()
        self.state.started_at = datetime.now(UTC)
        self.state.poller_enabled = True
        self._stop.clear()
        self._task = asyncio.create_task(self._refresh_loop(), name="poller-config")
        log.info("coletor iniciado: %d gateway(s), intervalo rápido %.2fs, lento %.0fs", len(self._runners), self.config.fast_interval_s, self.config.slow_interval_s)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for r in list(self._runners.values()):
            await r.stop()
        self._runners.clear()
        await self.writer.stop()
        self.pool.close_all()
        self.state.poller_enabled = False

    async def reload(self) -> None:
        """Relê gateways, medidores e configurações; aplica sem reiniciar o processo."""
        async with self.session_factory() as session:
            new = await load_config(session)
        old = self.config
        self.config = new
        self.pool.sync([g.as_dict() for g in new.gateways])
        wanted = {g.id: g for g in new.gateways}
        for gid in list(self._runners):
            if gid not in wanted:
                await self._runners.pop(gid).stop()
                self.state.gateways.pop(gid, None)
        for gid, g in wanted.items():
            runner = self._runners.get(gid)
            if runner is None:
                runner = GatewayRunner(g, self)
                self._runners[gid] = runner
                runner.start()
            else:
                runner.update_cfg(g)
        if old.raw_retention_days != new.raw_retention_days and old.gateways:
            log.info("retenção da tabela bruta ajustada para %d dias (aplicada pela manutenção periódica)", new.raw_retention_days)

    async def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config_refresh_s)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.reload()
            except Exception as exc:  # noqa: BLE001
                log.error("falha ao recarregar configuração: %s", exc)

    def gateway_client(self, gateway_id: int) -> GatewayClient | None:
        return self.pool.get(gateway_id)
