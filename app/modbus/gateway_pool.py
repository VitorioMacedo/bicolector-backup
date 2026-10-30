"""Mantém um GatewayClient por gateway cadastrado, acompanhando mudanças de configuração."""
from __future__ import annotations

import logging

from app.modbus.client import GatewayClient

log = logging.getLogger(__name__)


class GatewayPool:
    def __init__(self, timeout: float, retries: int):
        self.timeout = timeout
        self.retries = retries
        self._clients: dict[int, GatewayClient] = {}

    def get(self, gateway_id: int) -> GatewayClient | None:
        return self._clients.get(gateway_id)

    def sync(self, gateways: list[dict]) -> None:
        """Cria/atualiza/remove clientes para refletir a lista de gateways habilitados."""
        wanted = {g["id"]: g for g in gateways}
        for gid in list(self._clients):
            if gid not in wanted:
                log.info("gateway %s removido do pool", self._clients[gid].name)
                self._clients.pop(gid).close()
        for gid, g in wanted.items():
            current = self._clients.get(gid)
            if current is not None and current.same_endpoint(g["host"], g["port"], self.timeout):
                current.name = g["name"]
                continue
            if current is not None:
                current.close()
            self._clients[gid] = GatewayClient(gid, g["name"], g["host"], g["port"], timeout=self.timeout, retries=self.retries)

    def close_all(self) -> None:
        for c in self._clients.values():
            c.close()
        self._clients.clear()

    def __iter__(self):
        return iter(self._clients.values())
