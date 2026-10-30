"""Cliente Modbus TCP para um gateway (Link150/EGX150) com vários medidores RS-485 atrás dele.

Um gateway = uma conexão TCP + um lock: o barramento serial é half-duplex, então as requisições
para medidores do mesmo gateway são sempre sequenciais. Gateways diferentes rodam em paralelo.
"""
from __future__ import annotations

import asyncio
import logging
import time

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException, ModbusIOException

from app.modbus.registers import Block

log = logging.getLogger(__name__)


class ModbusError(Exception):
    """Erro de comunicação ou exceção Modbus devolvida pelo medidor/gateway."""


class GatewayClient:
    def __init__(self, gateway_id: int, name: str, host: str, port: int = 502, timeout: float = 0.3, retries: int = 1):
        self.gateway_id = gateway_id
        self.name = name
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retries = retries
        self.lock = asyncio.Lock()
        self._client = self._new_client()
        self.last_latency_ms: float = 0.0
        self.resets = 0

    def _new_client(self) -> AsyncModbusTcpClient:
        return AsyncModbusTcpClient(self.host, port=self.port, timeout=self.timeout, retries=0, reconnect_delay=0.5, reconnect_delay_max=10)

    def _reset_client(self, reason: str) -> None:
        """Descarta o cliente pymodbus (e seu estado de transações) após timeout ou resposta fora de sincronia."""
        self.resets += 1
        log.info("gateway %s: reabrindo conexão (%s)", self.name, reason)
        self.close()
        self._client = self._new_client()

    @property
    def hard_timeout(self) -> float:
        return self.timeout + 0.5

    @property
    def connected(self) -> bool:
        return self._client.connected

    def same_endpoint(self, host: str, port: int, timeout: float) -> bool:
        return self.host == host and self.port == port and abs(self.timeout - timeout) < 1e-9

    async def connect(self) -> bool:
        if self._client.connected:
            return True
        try:
            ok = await asyncio.wait_for(self._client.connect(), self.hard_timeout + 2.0)
        except asyncio.TimeoutError:
            log.warning("gateway %s: timeout ao conectar em %s:%s", self.name, self.host, self.port)
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("gateway %s: falha ao conectar em %s:%s (%s)", self.name, self.host, self.port, exc)
            return False
        if ok:
            log.info("gateway %s: conectado em %s:%s", self.name, self.host, self.port)
        return bool(ok)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    async def read_registers(self, unit: int, address0: int, count: int) -> list[int]:
        """FC03 com retry. `address0` é 0-based. Deve ser chamado com o lock do gateway."""
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            if not await self.connect():
                last_exc = ModbusError(f"gateway {self.name} desconectado")
                await asyncio.sleep(0.05)
                continue
            t0 = time.perf_counter()
            try:
                rr = await asyncio.wait_for(
                    self._client.read_holding_registers(address0, count=count, device_id=unit), self.hard_timeout
                )
            except ModbusIOException as exc:
                # Timeout / sem resposta: o gateway ainda pode responder a este pedido mais tarde. Se a conexão
                # fosse reaproveitada, essa resposta atrasada chegaria em paralelo ao próximo pedido e poderia
                # ser casada com o medidor errado. Reabrir a conexão garante que ela vá para um socket morto.
                self._reset_client(f"unit {unit} sem resposta ({str(exc).splitlines()[0][:60]})")
                last_exc = ModbusError(f"unit {unit}: timeout")
            except ModbusException as exc:
                last_exc = ModbusError(f"unit {unit}: {exc}")
            except asyncio.TimeoutError:
                # o pymodbus não devolveu nem erro dentro do prazo: estado interno suspeito, recria a conexão
                self._reset_client(f"unit {unit} sem resposta em {self.hard_timeout:.1f}s")
                last_exc = ModbusError(f"unit {unit}: timeout")
            except Exception as exc:  # noqa: BLE001
                last_exc = ModbusError(f"unit {unit}: {exc.__class__.__name__}: {exc}")
            else:
                self.last_latency_ms = (time.perf_counter() - t0) * 1000
                got_unit = getattr(rr, "dev_id", unit)
                if got_unit != unit:
                    # resposta de outro medidor (atrasada): nunca atribuir ao medidor errado
                    self._reset_client(f"resposta do unit {got_unit} recebida para o unit {unit}")
                    raise ModbusError(f"unit {unit}: resposta fora de sincronia (veio do unit {got_unit})")
                if rr.isError():
                    raise ModbusError(f"unit {unit}: exceção Modbus código {getattr(rr, 'exception_code', '?')}")
                if len(rr.registers) != count:
                    self._reset_client(f"unit {unit}: resposta com {len(rr.registers)} registradores, esperados {count}")
                    raise ModbusError(f"unit {unit}: resposta fora de sincronia (tamanho {len(rr.registers)} != {count})")
                return list(rr.registers)
            if attempt < self.retries:
                await asyncio.sleep(0.02)
        raise last_exc or ModbusError("falha desconhecida")

    async def read_block(self, unit: int, block: Block) -> dict[str, float | int | None]:
        regs = await self.read_registers(unit, block.address0, block.count)
        return block.decode(regs)

    async def write_registers(self, unit: int, address0: int, values: list[int]) -> None:
        """FC16. `address0` é 0-based. Deve ser chamado com o lock do gateway."""
        if not await self.connect():
            raise ModbusError(f"gateway {self.name} desconectado")
        try:
            rr = await asyncio.wait_for(self._client.write_registers(address0, values, device_id=unit), self.hard_timeout)
        except ModbusIOException as exc:
            self._reset_client(f"unit {unit} sem resposta à escrita")
            raise ModbusError(f"unit {unit}: timeout na escrita") from exc
        except ModbusException as exc:
            raise ModbusError(f"unit {unit}: {exc}") from exc
        except asyncio.TimeoutError as exc:
            self._reset_client(f"unit {unit} sem resposta à escrita em {self.hard_timeout:.1f}s")
            raise ModbusError(f"unit {unit}: timeout na escrita") from exc
        if rr.isError():
            raise ModbusError(f"unit {unit}: escrita recusada, exceção Modbus código {getattr(rr, 'exception_code', '?')}")
