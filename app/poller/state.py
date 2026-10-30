"""Estado em memória do coletor, exposto ao painel/API sem bater no banco."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class MeterLive:
    meter_id: int
    name: str
    unit_id: int
    gateway_id: int
    online: bool = False
    consecutive_failures: int = 0
    last_ok_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    last_fast: dict = field(default_factory=dict)
    last_fast_at: datetime | None = None
    last_slow: dict = field(default_factory=dict)
    last_slow_at: datetime | None = None
    last_latency_ms: float = 0.0
    next_retry_at: float = 0.0  # time.monotonic()

    def to_dict(self) -> dict:
        return {
            "meter_id": self.meter_id,
            "name": self.name,
            "unit_id": self.unit_id,
            "gateway_id": self.gateway_id,
            "online": self.online,
            "consecutive_failures": self.consecutive_failures,
            "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "last_fast": self.last_fast,
            "last_fast_at": self.last_fast_at.isoformat() if self.last_fast_at else None,
            "last_slow": self.last_slow,
            "last_slow_at": self.last_slow_at.isoformat() if self.last_slow_at else None,
            "last_latency_ms": round(self.last_latency_ms, 1),
        }


@dataclass
class GatewayLive:
    gateway_id: int
    name: str
    host: str
    port: int
    connected: bool = False
    fast_block: str = "full"
    last_cycle_ms: float = 0.0
    avg_cycle_ms: float = 0.0
    max_cycle_ms: float = 0.0
    interval_ms: float = 1000.0
    cycles: int = 0
    overruns: int = 0  # ciclos que passaram do intervalo configurado
    last_cycle_at: datetime | None = None
    meters: dict[int, MeterLive] = field(default_factory=dict)

    def record_cycle(self, duration_ms: float, interval_ms: float, at: datetime) -> None:
        self.cycles += 1
        self.last_cycle_ms = duration_ms
        self.interval_ms = interval_ms
        self.last_cycle_at = at
        self.max_cycle_ms = max(self.max_cycle_ms, duration_ms)
        alpha = 0.05
        self.avg_cycle_ms = duration_ms if self.cycles == 1 else (1 - alpha) * self.avg_cycle_ms + alpha * duration_ms
        if duration_ms > interval_ms:
            self.overruns += 1

    def to_dict(self) -> dict:
        return {
            "gateway_id": self.gateway_id,
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "connected": self.connected,
            "fast_block": self.fast_block,
            "last_cycle_ms": round(self.last_cycle_ms, 1),
            "avg_cycle_ms": round(self.avg_cycle_ms, 1),
            "max_cycle_ms": round(self.max_cycle_ms, 1),
            "interval_ms": self.interval_ms,
            "cycles": self.cycles,
            "overruns": self.overruns,
            "overrun": self.last_cycle_ms > self.interval_ms,
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "meters": [m.to_dict() for m in self.meters.values()],
        }


class RuntimeState:
    def __init__(self) -> None:
        self.gateways: dict[int, GatewayLive] = {}
        self.started_at: datetime | None = None
        self.poller_enabled: bool = False

    def meter(self, meter_id: int) -> MeterLive | None:
        for g in self.gateways.values():
            if meter_id in g.meters:
                return g.meters[meter_id]
        return None

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "poller_enabled": self.poller_enabled,
            "gateways": [g.to_dict() for g in self.gateways.values()],
        }
