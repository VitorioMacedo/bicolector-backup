"""Comandos para o medidor pela "command interface" do PM2xxx (reset de energia, demanda, mín/máx).

Duas interfaces existem no PM2xxx:
  - sem semáforo: bloco escrito a partir de 5249/5250 (1-based), resultado em 5376;
  - protegida: ler semáforo em 5680, escrever bloco em 5000 com [comando, semáforo], resultado em 5126.
Tentamos a primeira; se o medidor recusar (exceção Modbus) ou devolver resultado != 0, tentamos a protegida.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.modbus import registers as R
from app.modbus.client import GatewayClient, ModbusError

log = logging.getLogger(__name__)


@dataclass
class CommandResult:
    success: bool
    result_code: int | None
    detail: str


async def send_command(
    gw: GatewayClient,
    unit: int,
    code: int,
    password: int = 0,
    layout: str = "password_first",
    params: list[int] | None = None,
) -> CommandResult:
    """Envia um comando ao medidor. Deve ser chamado com `gw.lock` adquirido (o poller é pausado)."""
    params = params or []
    attempts: list[str] = []

    # 1) interface sem semáforo
    if layout == "command_first":
        addr0, values = R.CMD_UNPROTECTED_COMMAND - 1, [code, 0, *params]
    else:
        addr0, values = R.CMD_UNPROTECTED_PASSWORD - 1, [password, code, 0, *params]
    try:
        await gw.write_registers(unit, addr0, values)
        result = (await gw.read_registers(unit, R.CMD_UNPROTECTED_RESULT - 1, 1))[0]
        if result == 0:
            return CommandResult(True, 0, f"comando {code} aceito (interface sem semáforo, layout {layout})")
        attempts.append(f"sem semáforo: resultado {result}")
    except ModbusError as exc:
        attempts.append(f"sem semáforo: {exc}")

    # 2) interface protegida por semáforo
    try:
        semaphore = (await gw.read_registers(unit, R.CMD_SEMAPHORE - 1, 1))[0]
        await gw.write_registers(unit, R.CMD_PROTECTED_COMMAND - 1, [code, semaphore, *params])
        result = (await gw.read_registers(unit, R.CMD_PROTECTED_RESULT - 1, 1))[0]
        if result == 0:
            return CommandResult(True, 0, f"comando {code} aceito (interface protegida, semáforo {semaphore})")
        attempts.append(f"protegida: resultado {result}")
        return CommandResult(False, result, "; ".join(attempts))
    except ModbusError as exc:
        attempts.append(f"protegida: {exc}")
        return CommandResult(False, None, "; ".join(attempts))


async def read_identification(gw: GatewayClient, unit: int) -> dict[str, str | int | None]:
    """Número de série, firmware e relação de TC. Deve ser chamado com `gw.lock` adquirido."""
    out: dict[str, str | int | None] = {"serial_number": None, "firmware": None, "ct_primary": None, "ct_secondary": None}
    try:
        d = await gw.read_block(unit, R.IDENT_SERIAL)
        out["serial_number"] = str(d["serial_number"])
    except ModbusError as exc:
        log.info("unit %s: serial indisponível (%s)", unit, exc)
    try:
        d = await gw.read_block(unit, R.IDENT_FIRMWARE)
        out["firmware"] = R.format_firmware(int(d["firmware_raw"]))
    except ModbusError as exc:
        log.info("unit %s: firmware indisponível (%s)", unit, exc)
    try:
        d = await gw.read_block(unit, R.IDENT_CT)
        out["ct_primary"] = int(d["ct_primary"])
        out["ct_secondary"] = int(d["ct_secondary"])
    except ModbusError as exc:
        log.info("unit %s: TC indisponível (%s)", unit, exc)
    return out
