"""Freio de força bruta no login: N falhas por IP bloqueiam novas tentativas por alguns minutos (em memória)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.config import get_settings


@dataclass
class _Entry:
    failures: int = 0
    blocked_until: float = 0.0
    last: float = 0.0


@dataclass
class LoginThrottle:
    _by_ip: dict[str, _Entry] = field(default_factory=dict)

    def blocked_for(self, ip: str) -> int:
        """Segundos restantes de bloqueio (0 = liberado)."""
        e = self._by_ip.get(ip)
        if e is None:
            return 0
        rest = e.blocked_until - time.monotonic()
        if rest <= 0:
            if e.blocked_until:
                self._by_ip.pop(ip, None)
            return 0
        return int(rest) + 1

    def record_failure(self, ip: str) -> int:
        """Registra uma falha; devolve os segundos de bloqueio que ela causou (0 se ainda não bloqueou)."""
        s = get_settings()
        window = s.login_lockout_minutes * 60
        now = time.monotonic()
        self._sweep(now, window)
        e = self._by_ip.setdefault(ip, _Entry())
        e.failures += 1
        e.last = now
        if e.failures >= s.login_max_attempts:
            e.failures = 0
            e.blocked_until = now + window
            return window
        return 0

    def reset(self, ip: str) -> None:
        self._by_ip.pop(ip, None)

    def _sweep(self, now: float, window: float) -> None:
        """Esquece IPs sem atividade há mais de uma janela: falhas antigas não contam e o dict não cresce sem limite."""
        for ip, e in list(self._by_ip.items()):
            if max(e.blocked_until, e.last + window) <= now:
                del self._by_ip[ip]


throttle = LoginThrottle()
