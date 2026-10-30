"""Modelo de dados (SQL Server em produção, SQLite nos testes).

Índices, compressão, tabelas de agregados e views ficam em sql/mssql.sql, aplicado depois do create_all.
"""
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.dialects.mssql import DATETIME2
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UtcDateTime(TypeDecorator):
    """Instante guardado como DATETIME2 em UTC (sem fuso) e devolvido como datetime com tzinfo=UTC.

    Evita o DATETIMEOFFSET (mal suportado pelo pyodbc e pelo Power BI em DirectQuery) e mantém o resto do
    código trabalhando só com datetimes conscientes de fuso.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "mssql":
            return dialect.type_descriptor(DATETIME2(3))
        return dialect.type_descriptor(DateTime())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Gateway(Base):
    __tablename__ = "gateway"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    host: Mapped[str] = mapped_column(String(120), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=502)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # "full" = registradores 3000-3093 (94 regs); "compact" = 3010-3085 (76 regs, só médias de I e V)
    fast_block: Mapped[str] = mapped_column(String(10), nullable=False, default="full")
    # cor dos gráficos (#rrggbb); preenchida pela paleta quando o cadastro não informa
    color: Mapped[str | None] = mapped_column(String(7))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    meters: Mapped[list["Meter"]] = relationship(back_populates="gateway", cascade="all, delete-orphan")


class Meter(Base):
    __tablename__ = "meter"
    __table_args__ = (UniqueConstraint("gateway_id", "unit_id", name="uq_meter_gateway_unit"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gateway_id: Mapped[int] = mapped_column(ForeignKey("gateway.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    unit_id: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ct_primary: Mapped[int | None] = mapped_column(Integer)
    serial_number: Mapped[str | None] = mapped_column(String(40))
    firmware: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    gateway: Mapped[Gateway] = relationship(back_populates="meters")


class AppSetting(Base):
    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


FAST_COLUMNS = [
    "ia", "ib", "ic", "in_", "i_avg",
    "vab", "vbc", "vca", "vll_avg",
    "van", "vbn", "vcn", "vln_avg",
    "p_a", "p_b", "p_c", "p_total", "q_total", "s_total",
    "pf_total", "pf_disp",
]

SLOW_COLUMNS = [
    "freq", "demand_present", "demand_last", "demand_peak",
    "kwh_import", "kwh_export", "kwh_net", "kvarh", "kvah",
    "thd_ia", "thd_ib", "thd_ic", "thd_van", "thd_vbn", "thd_vcn",
]


class ReadingFast(Base):
    """Ciclo rápido (padrão 1 s): corrente, tensão, potência, fator de potência."""

    __tablename__ = "reading_fast"

    ts: Mapped[datetime] = mapped_column(UtcDateTime, primary_key=True)
    meter_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ia: Mapped[float | None] = mapped_column(Float)
    ib: Mapped[float | None] = mapped_column(Float)
    ic: Mapped[float | None] = mapped_column(Float)
    in_: Mapped[float | None] = mapped_column("in_", Float)
    i_avg: Mapped[float | None] = mapped_column(Float)
    vab: Mapped[float | None] = mapped_column(Float)
    vbc: Mapped[float | None] = mapped_column(Float)
    vca: Mapped[float | None] = mapped_column(Float)
    vll_avg: Mapped[float | None] = mapped_column(Float)
    van: Mapped[float | None] = mapped_column(Float)
    vbn: Mapped[float | None] = mapped_column(Float)
    vcn: Mapped[float | None] = mapped_column(Float)
    vln_avg: Mapped[float | None] = mapped_column(Float)
    p_a: Mapped[float | None] = mapped_column(Float)
    p_b: Mapped[float | None] = mapped_column(Float)
    p_c: Mapped[float | None] = mapped_column(Float)
    p_total: Mapped[float | None] = mapped_column(Float)
    q_total: Mapped[float | None] = mapped_column(Float)
    s_total: Mapped[float | None] = mapped_column(Float)
    pf_total: Mapped[float | None] = mapped_column(Float)
    pf_disp: Mapped[float | None] = mapped_column(Float)


class ReadingSlow(Base):
    """Ciclo lento (padrão 30 s): energia acumulada, demanda, frequência, THD."""

    __tablename__ = "reading_slow"

    ts: Mapped[datetime] = mapped_column(UtcDateTime, primary_key=True)
    meter_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    freq: Mapped[float | None] = mapped_column(Float)
    demand_present: Mapped[float | None] = mapped_column(Float)
    demand_last: Mapped[float | None] = mapped_column(Float)
    demand_peak: Mapped[float | None] = mapped_column(Float)
    kwh_import: Mapped[float | None] = mapped_column(Float)
    kwh_export: Mapped[float | None] = mapped_column(Float)
    kwh_net: Mapped[float | None] = mapped_column(Float)
    kvarh: Mapped[float | None] = mapped_column(Float)
    kvah: Mapped[float | None] = mapped_column(Float)
    thd_ia: Mapped[float | None] = mapped_column(Float)
    thd_ib: Mapped[float | None] = mapped_column(Float)
    thd_ic: Mapped[float | None] = mapped_column(Float)
    thd_van: Mapped[float | None] = mapped_column(Float)
    thd_vbn: Mapped[float | None] = mapped_column(Float)
    thd_vcn: Mapped[float | None] = mapped_column(Float)


class PollCycle(Base):
    """Uma linha por ciclo rápido de cada gateway: diagnóstico do intervalo alcançável."""

    __tablename__ = "poll_cycle"

    ts: Mapped[datetime] = mapped_column(UtcDateTime, primary_key=True)
    gateway_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    interval_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    meters_ok: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    meters_failed: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    slow_reads: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)


class MeterStatus(Base):
    __tablename__ = "meter_status"

    meter_id: Mapped[int] = mapped_column(ForeignKey("meter.id", ondelete="CASCADE"), primary_key=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


class CommandLog(Base):
    __tablename__ = "command_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    meter_id: Mapped[int | None] = mapped_column(ForeignKey("meter.id", ondelete="SET NULL"))
    command: Mapped[str] = mapped_column(String(40), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(80), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    result_code: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(Text)
