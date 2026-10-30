from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class GatewayIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=120)
    port: int = Field(default=502, ge=1, le=65535)
    fast_block: str = "full"
    enabled: bool = True
    # cor dos gráficos (#rrggbb); vazio = a aplicação escolhe uma cor da paleta
    color: str | None = Field(default=None, pattern=r"^(#[0-9a-fA-F]{6})?$")


class GatewayPatch(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    fast_block: str | None = None
    enabled: bool | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class GatewayOut(BaseModel):
    id: int
    name: str
    host: str
    port: int
    fast_block: str
    enabled: bool
    color: str | None

    model_config = {"from_attributes": True}


class MeterIn(BaseModel):
    gateway_id: int
    name: str = Field(min_length=1, max_length=80)
    unit_id: int = Field(ge=1, le=247)
    enabled: bool = True


class MeterPatch(BaseModel):
    name: str | None = None
    unit_id: int | None = Field(default=None, ge=1, le=247)
    enabled: bool | None = None
    gateway_id: int | None = None


class MeterOut(BaseModel):
    id: int
    gateway_id: int
    name: str
    unit_id: int
    enabled: bool
    ct_primary: int | None
    serial_number: str | None
    firmware: str | None

    model_config = {"from_attributes": True}


class CommandOut(BaseModel):
    id: int
    ts: datetime
    meter_id: int | None
    command: str
    requested_by: str
    success: bool
    result_code: int | None
    detail: str | None

    model_config = {"from_attributes": True}
