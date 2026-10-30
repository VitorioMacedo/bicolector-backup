"""Snapshot da configuração (tabelas gateway, meter e app_setting) usado pelo poller."""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSetting, Gateway, Meter


@dataclass(frozen=True)
class MeterCfg:
    id: int
    name: str
    unit_id: int
    gateway_id: int


@dataclass(frozen=True)
class GatewayCfg:
    id: int
    name: str
    host: str
    port: int
    fast_block: str
    meters: tuple[MeterCfg, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "host": self.host, "port": self.port}


@dataclass(frozen=True)
class PollConfig:
    fast_interval_s: float = 1.0
    slow_interval_s: float = 30.0
    offline_after_failures: int = 5
    raw_retention_days: int = 90
    reset_command_code: int = 2020
    reset_password: int = 0
    reset_layout: str = "password_first"
    gateways: tuple[GatewayCfg, ...] = field(default_factory=tuple)

    def gateway(self, gateway_id: int) -> GatewayCfg | None:
        return next((g for g in self.gateways if g.id == gateway_id), None)


def _f(settings: dict[str, str], key: str, default: float) -> float:
    try:
        return float(settings.get(key, default))
    except ValueError:
        return default


def _i(settings: dict[str, str], key: str, default: int) -> int:
    try:
        return int(float(settings.get(key, default)))
    except ValueError:
        return default


async def load_config(session: AsyncSession) -> PollConfig:
    settings = {row.key: row.value for row in (await session.execute(select(AppSetting))).scalars()}
    gateways = (await session.execute(select(Gateway).where(Gateway.enabled == true()).order_by(Gateway.id))).scalars().all()
    meters = (await session.execute(select(Meter).where(Meter.enabled == true()).order_by(Meter.gateway_id, Meter.unit_id))).scalars().all()
    by_gw: dict[int, list[MeterCfg]] = {}
    for m in meters:
        by_gw.setdefault(m.gateway_id, []).append(MeterCfg(m.id, m.name, m.unit_id, m.gateway_id))
    gw_cfgs = tuple(
        GatewayCfg(g.id, g.name, g.host, g.port, g.fast_block if g.fast_block in ("full", "compact") else "full", tuple(by_gw.get(g.id, [])))
        for g in gateways
    )
    return PollConfig(
        fast_interval_s=max(0.2, _f(settings, "fast_interval_s", 1.0)),
        slow_interval_s=max(1.0, _f(settings, "slow_interval_s", 30.0)),
        offline_after_failures=max(1, _i(settings, "offline_after_failures", 5)),
        raw_retention_days=max(1, _i(settings, "raw_retention_days", 90)),
        reset_command_code=_i(settings, "reset_command_code", 2020),
        reset_password=_i(settings, "reset_password", 0),
        reset_layout=settings.get("reset_layout", "password_first"),
        gateways=gw_cfgs,
    )
