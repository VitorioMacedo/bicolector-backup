"""Mapa de registradores do Schneider EasyLogic PM2230 (série PM2200).

Endereços abaixo são os do manual Schneider (1-based). Na requisição Modbus usa-se endereço - 1.
Todas as grandezas de medição são Float32 (2 registradores, big-endian). Valores não disponíveis
chegam como NaN e são gravados como NULL.

Fonte: manual EasyLogic PM2200 series (NHA2778902) e planilha de registradores PM2xxx (se.com).
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Field:
    name: str        # nome da coluna no banco
    reg: int         # endereço 1-based do primeiro registrador
    kind: str = "f32"  # f32 | u32 | u16 | raw2


@dataclass(frozen=True)
class Block:
    """Um bloco contíguo lido com uma única requisição FC03."""

    name: str
    start: int                 # 1-based
    count: int                 # número de registradores (<= 125)
    fields: tuple[Field, ...] = field(default_factory=tuple)

    @property
    def address0(self) -> int:
        return self.start - 1

    def decode(self, regs: list[int]) -> dict[str, float | int | None]:
        if len(regs) < self.count:
            raise ValueError(f"bloco {self.name}: esperados {self.count} registradores, recebidos {len(regs)}")
        out: dict[str, float | int | None] = {}
        for f in self.fields:
            off = f.reg - self.start
            if f.kind == "f32":
                out[f.name] = decode_f32(regs[off], regs[off + 1])
            elif f.kind == "u32":
                out[f.name] = (regs[off] << 16) | regs[off + 1]
            elif f.kind == "u16":
                out[f.name] = regs[off]
            elif f.kind == "raw2":
                out[f.name] = (regs[off] << 16) | regs[off + 1]
        return out


def decode_f32(hi: int, lo: int) -> float | None:
    """Float32 IEEE-754 com word order big-endian (hi primeiro), como no PM2xxx."""
    value = struct.unpack(">f", struct.pack(">HH", hi & 0xFFFF, lo & 0xFFFF))[0]
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def encode_f32(value: float) -> tuple[int, int]:
    hi, lo = struct.unpack(">HH", struct.pack(">f", value))
    return hi, lo


# ---------------------------------------------------------------------------
# Ciclo rápido (padrão 1 s)
# ---------------------------------------------------------------------------
_FAST_FIELDS = (
    Field("ia", 3000), Field("ib", 3002), Field("ic", 3004), Field("in_", 3006), Field("i_avg", 3010),
    Field("vab", 3020), Field("vbc", 3022), Field("vca", 3024), Field("vll_avg", 3026),
    Field("van", 3028), Field("vbn", 3030), Field("vcn", 3032), Field("vln_avg", 3036),
    Field("p_a", 3054), Field("p_b", 3056), Field("p_c", 3058), Field("p_total", 3060),
    Field("q_total", 3068), Field("s_total", 3076),
    Field("pf_total", 3084), Field("pf_disp", 3092),
)

FAST_FULL = Block("fast_full", start=3000, count=94, fields=_FAST_FIELDS)

# Bloco reduzido para o gateway mais carregado: só médias de corrente/tensão (76 registradores).
FAST_COMPACT = Block(
    "fast_compact",
    start=3010,
    count=76,
    fields=tuple(f for f in _FAST_FIELDS if 3010 <= f.reg and f.reg + 1 < 3010 + 76),
)

FAST_BLOCKS: dict[str, Block] = {"full": FAST_FULL, "compact": FAST_COMPACT}

# ---------------------------------------------------------------------------
# Ciclo lento (padrão 30 s): 3 requisições por medidor
# ---------------------------------------------------------------------------
SLOW_DEMAND = Block(
    "slow_demand",
    start=3110,
    count=46,
    fields=(
        Field("freq", 3110),
        Field("demand_present", 3142), Field("demand_last", 3148), Field("demand_peak", 3154),
    ),
)
SLOW_ENERGY = Block(
    "slow_energy",
    start=3204,
    count=20,
    fields=(
        Field("kwh_import", 3204), Field("kwh_export", 3206), Field("kvarh", 3208),
        Field("kvah", 3214), Field("kwh_net", 3222),
    ),
)
SLOW_THD = Block(
    "slow_thd",
    start=2130,
    count=14,
    fields=(
        Field("thd_ia", 2130), Field("thd_ib", 2132), Field("thd_ic", 2134),
        Field("thd_van", 2138), Field("thd_vbn", 2140), Field("thd_vcn", 2142),
    ),
)
SLOW_BLOCKS: tuple[Block, ...] = (SLOW_DEMAND, SLOW_ENERGY, SLOW_THD)

# ---------------------------------------------------------------------------
# Identificação (lida sob demanda)
# ---------------------------------------------------------------------------
IDENT_SERIAL = Block("ident_serial", start=130, count=2, fields=(Field("serial_number", 130, "u32"),))
IDENT_FIRMWARE = Block("ident_firmware", start=1637, count=3, fields=(Field("firmware_raw", 1637, "raw2"), Field("hardware_rev", 1639, "u16")))
IDENT_CT = Block("ident_ct", start=2004, count=2, fields=(Field("ct_primary", 2004, "u16"), Field("ct_secondary", 2005, "u16")))


def format_firmware(raw: int) -> str:
    """Firmware vem empacotado em 2 registradores (ex.: 0x0105 0x0003 -> '1.5.3')."""
    hi, lo = (raw >> 16) & 0xFFFF, raw & 0xFFFF
    return f"{hi >> 8}.{hi & 0xFF}.{lo}"


# ---------------------------------------------------------------------------
# Interface de comandos (reset de energia etc.) — endereços 1-based
# ---------------------------------------------------------------------------
CMD_UNPROTECTED_PASSWORD = 5249   # layout password_first: [senha, comando, 0...] a partir daqui
CMD_UNPROTECTED_COMMAND = 5250    # layout command_first: [comando, 0, params...] a partir daqui
CMD_UNPROTECTED_RESULT = 5376     # 0 = comando aceito
CMD_SEMAPHORE = 5680              # interface protegida: ler 1x, válido 4 min
CMD_PROTECTED_COMMAND = 5000      # [comando, semáforo, params...]
CMD_PROTECTED_RESULT = 5126       # 0 = comando aceito

# Códigos conhecidos. O de reset de energia diverge entre fontes (2020 na FAQ Schneider / 1003 em
# projeto comunitário); por isso é configuração (app_setting.reset_command_code) e deve ser validado
# em bancada contra a planilha oficial de registradores.
COMMAND_RESET_ENERGY_DEFAULT = 2020
COMMAND_RESET_DEMAND_DEFAULT = 1001
COMMAND_RESET_MINMAX_DEFAULT = 1002
