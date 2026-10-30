"""Gravação em lote: as leituras de um ciclo são acumuladas e gravadas numa única transação por segundo."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import FAST_COLUMNS, SLOW_COLUMNS, Meter, MeterStatus, PollCycle, ReadingFast, ReadingSlow

log = logging.getLogger(__name__)

_FAST_KEYS = ("ts", "meter_id", *FAST_COLUMNS)
_SLOW_KEYS = ("ts", "meter_id", *SLOW_COLUMNS)
_STATUS_KEYS = ("last_ok_at", "last_error", "last_error_at", "consecutive_failures")

# Upsert do status no SQL Server: os campos ausentes no lote (None na linha) mantêm o valor que já está no banco.
_STATUS_MERGE = text("""
MERGE meter_status AS t
USING (SELECT :meter_id AS meter_id) AS s ON t.meter_id = s.meter_id
WHEN MATCHED THEN UPDATE SET
  last_ok_at = COALESCE(:last_ok_at, t.last_ok_at),
  last_error = CASE WHEN :has_error = 1 THEN :last_error ELSE t.last_error END,
  last_error_at = COALESCE(:last_error_at, t.last_error_at),
  consecutive_failures = COALESCE(:consecutive_failures, t.consecutive_failures),
  updated_at = :now
WHEN NOT MATCHED THEN INSERT (meter_id, last_ok_at, last_error, last_error_at, consecutive_failures, updated_at)
  VALUES (:meter_id, :last_ok_at, :last_error, :last_error_at, COALESCE(:consecutive_failures, 0), :now);
""")


def _normalize(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    """executemany exige o mesmo conjunto de chaves em todas as linhas (o bloco compact tem menos colunas)."""
    return [{k: r.get(k) for k in keys} for r in rows]


def _utc_naive(value: datetime | None) -> datetime | None:
    """Parâmetros de text(): o SQL Server guarda DATETIME2 em UTC sem fuso (ver models.UtcDateTime)."""
    if value is None:
        return None
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


class BatchWriter:
    def __init__(self, engine: AsyncEngine, flush_interval: float = 1.0):
        self.engine = engine
        self.flush_interval = flush_interval
        self._fast: list[dict] = []
        self._slow: list[dict] = []
        self._cycles: list[dict] = []
        self._status: dict[int, dict] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.rows_written = 0
        self.last_flush_at: datetime | None = None
        self.last_error: str | None = None

    # -- enfileiramento (chamado pelos runners) --------------------------------
    def add_fast(self, row: dict) -> None:
        self._fast.append(row)

    def add_slow(self, row: dict) -> None:
        self._slow.append(row)

    def add_cycle(self, row: dict) -> None:
        self._cycles.append(row)

    def set_status(self, meter_id: int, **fields) -> None:
        self._status.setdefault(meter_id, {"meter_id": meter_id}).update(fields)

    @property
    def pending(self) -> int:
        return len(self._fast) + len(self._slow) + len(self._cycles) + len(self._status)

    # -- ciclo de vida ---------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="batch-writer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
        await self.flush()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.flush_interval)
            except asyncio.TimeoutError:
                pass
            try:
                await self.flush()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                log.error("falha ao gravar lote (%d linhas pendentes): %s", self.pending, self.last_error)
                if len(self._fast) > 50_000:  # banco fora por muito tempo: descarta o mais antigo
                    del self._fast[: len(self._fast) - 50_000]

    def _insert(self, model):
        # Duplicatas (lote reenviado depois de uma queda de conexão): no SQL Server a chave primária tem
        # IGNORE_DUP_KEY = ON (sql/mssql.sql) e a linha repetida é descartada; no SQLite (testes) OR IGNORE.
        stmt = insert(model)
        return stmt.prefix_with("OR IGNORE") if self.engine.dialect.name == "sqlite" else stmt

    async def flush(self) -> None:
        fast, slow, cycles, status = self._fast, self._slow, self._cycles, self._status
        if not (fast or slow or cycles or status):
            return
        self._fast, self._slow, self._cycles, self._status = [], [], [], {}
        try:
            async with self.engine.begin() as conn:
                if fast:
                    await conn.execute(self._insert(ReadingFast), _normalize(fast, _FAST_KEYS))
                if slow:
                    await conn.execute(self._insert(ReadingSlow), _normalize(slow, _SLOW_KEYS))
                if cycles:
                    await conn.execute(self._insert(PollCycle), cycles)
        except Exception:
            # devolve para a fila para tentar de novo no próximo flush
            self._fast = fast + self._fast
            self._slow = slow + self._slow
            self._cycles = cycles + self._cycles
            for k, v in status.items():
                self._status.setdefault(k, v)
            raise
        if status:
            await self._write_status(status)
        self.rows_written += len(fast) + len(slow)
        self.last_flush_at = datetime.now().astimezone()
        self.last_error = None

    async def _write_status(self, status: dict[int, dict]) -> None:
        # Em transação própria e só para medidores que ainda existem: um medidor apagado com status pendente
        # violaria a chave estrangeira de meter_status e, se estivesse junto das leituras, travaria o lote
        # inteiro para sempre.
        now = datetime.now(UTC)
        async with self.engine.begin() as conn:
            existing = set((await conn.execute(select(Meter.id).where(Meter.id.in_(list(status))))).scalars())
            for meter_id, st in status.items():
                if meter_id not in existing:
                    log.debug("status do medidor %s descartado: medidor não existe mais", meter_id)
                    continue
                if self.engine.dialect.name == "mssql":
                    params = {k: st.get(k) for k in _STATUS_KEYS}
                    params["last_ok_at"] = _utc_naive(params["last_ok_at"])
                    params["last_error_at"] = _utc_naive(params["last_error_at"])
                    params.update(meter_id=meter_id, has_error=1 if "last_error" in st else 0, now=_utc_naive(now))
                    await conn.execute(_STATUS_MERGE, params)
                else:
                    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

                    update = {k: v for k, v in st.items() if k != "meter_id"}
                    stmt = sqlite_insert(MeterStatus).values(updated_at=now, **st)
                    await conn.execute(stmt.on_conflict_do_update(index_elements=["meter_id"], set_={**update, "updated_at": now}))
