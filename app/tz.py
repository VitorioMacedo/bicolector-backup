"""Fuso horário das views do Power BI.

A configuração `display_timezone` usa nomes IANA (America/Cuiaba). O SQL Server só conhece os nomes de fuso do
Windows (`AT TIME ZONE 'Central Brazilian Standard Time'`), então a aplicação grava o equivalente em
`display_timezone_windows`, que as views v_pbi_* leem.
"""
from __future__ import annotations

WINDOWS_TZ: dict[str, str] = {
    "UTC": "UTC",
    "Etc/UTC": "UTC",
    # Brasil
    "America/Sao_Paulo": "E. South America Standard Time",
    "America/Bahia": "Bahia Standard Time",
    "America/Fortaleza": "SA Eastern Standard Time",
    "America/Recife": "SA Eastern Standard Time",
    "America/Maceio": "SA Eastern Standard Time",
    "America/Belem": "SA Eastern Standard Time",
    "America/Santarem": "SA Eastern Standard Time",
    "America/Araguaina": "Tocantins Standard Time",
    "America/Cuiaba": "Central Brazilian Standard Time",
    "America/Campo_Grande": "Central Brazilian Standard Time",
    "America/Manaus": "SA Western Standard Time",
    "America/Porto_Velho": "SA Western Standard Time",
    "America/Boa_Vista": "SA Western Standard Time",
    "America/Rio_Branco": "SA Pacific Standard Time",
    "America/Eirunepe": "SA Pacific Standard Time",
    "America/Noronha": "UTC-02",
    # vizinhos
    "America/Argentina/Buenos_Aires": "Argentina Standard Time",
    "America/Asuncion": "Paraguay Standard Time",
    "America/Montevideo": "Montevideo Standard Time",
    "America/Santiago": "Pacific SA Standard Time",
    "America/La_Paz": "SA Western Standard Time",
    "America/Lima": "SA Pacific Standard Time",
    "America/Bogota": "SA Pacific Standard Time",
    "America/Caracas": "Venezuela Standard Time",
    "America/New_York": "Eastern Standard Time",
    "America/Chicago": "Central Standard Time",
    "America/Denver": "Mountain Standard Time",
    "America/Los_Angeles": "Pacific Standard Time",
    "Europe/Lisbon": "GMT Standard Time",
    "Europe/London": "GMT Standard Time",
    "Europe/Madrid": "Romance Standard Time",
    "Europe/Paris": "Romance Standard Time",
    "Europe/Berlin": "W. Europe Standard Time",
}
DEFAULT_WINDOWS_TZ = "Central Brazilian Standard Time"


def windows_timezone(iana: str) -> str | None:
    """Nome do fuso no SQL Server para um nome IANA; None se não houver equivalente mapeado."""
    return WINDOWS_TZ.get(iana)
