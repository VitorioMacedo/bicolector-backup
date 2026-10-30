"""Criação idempotente do esquema: tabelas (SQLAlchemy) + índices, agregados e views (sql/mssql.sql)."""
import logging
from pathlib import Path

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import AppSetting, Base
from app.tz import DEFAULT_WINDOWS_TZ, windows_timezone

log = logging.getLogger(__name__)
SQL_FILE = Path(__file__).resolve().parents[2] / "sql" / "mssql.sql"

# Valores iniciais da tabela app_setting (editáveis no painel). reset_layout:
#   password_first -> escreve [senha, comando, 0] a partir do registrador 5249 (1-based)
#   command_first  -> escreve [comando, 0] a partir do registrador 5250 (1-based)
DEFAULT_SETTINGS: dict[str, str] = {
    "fast_interval_s": "1.0",
    "slow_interval_s": "30.0",
    "raw_retention_days": "90",
    "offline_after_failures": "5",
    "reset_command_code": "2020",
    "reset_password": "0",
    "reset_layout": "password_first",
    # fuso usado pelas views v_pbi_* (horário local nos gráficos do Power BI); o nome equivalente do Windows
    # (display_timezone_windows) é mantido pela aplicação, porque o SQL Server só conhece esses nomes.
    "display_timezone": "America/Cuiaba",
    "display_timezone_windows": DEFAULT_WINDOWS_TZ,
}


def split_statements(sql: str) -> list[str]:
    """Divide o arquivo SQL em comandos pelo marcador '-- @@', descartando linhas de comentário."""
    out: list[str] = []
    for part in sql.split("-- @@"):
        lines = [ln for ln in part.splitlines() if not ln.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if stmt:
            out.append(stmt)
    return out


async def init_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        existing = set((await conn.execute(select(AppSetting.key))).scalars())
        for key, value in DEFAULT_SETTINGS.items():
            if key not in existing:
                await conn.execute(insert(AppSetting).values(key=key, value=value))
    if engine.dialect.name == "mssql":
        autocommit = engine.execution_options(isolation_level="AUTOCOMMIT")
        async with autocommit.connect() as conn:
            for stmt in split_statements(SQL_FILE.read_text(encoding="utf-8")):
                try:
                    await conn.execute(text(stmt))
                except Exception as exc:  # noqa: BLE001 - registra e segue; o restante do esquema ainda é útil
                    first_line = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
                    log.warning("mssql.sql: comando ignorado: %s | %s", first_line, stmt.splitlines()[0][:80])
    else:
        log.warning("banco %s: índices, agregados e views de sql/mssql.sql não aplicados (só SQL Server)", engine.dialect.name)
    await _backfill_gateway_colors(engine)
    await sync_display_timezone(engine)
    log.info("esquema do banco verificado")


async def _backfill_gateway_colors(engine: AsyncEngine) -> None:
    """Gateways criados antes da coluna `color` recebem uma cor da paleta (a API nunca devolve cor nula)."""
    from app.services import next_gateway_color  # import tardio: services importa este módulo

    async with engine.begin() as conn:
        rows = (await conn.execute(text("SELECT id, color FROM gateway ORDER BY id"))).all()
        used = [c for _, c in rows if c]
        for gid, color in rows:
            if color:
                continue
            new = next_gateway_color(used)
            used.append(new)
            await conn.execute(text("UPDATE gateway SET color = :c WHERE id = :id"), {"c": new, "id": gid})


async def sync_display_timezone(engine: AsyncEngine) -> None:
    """Mantém display_timezone_windows (lido pelas views do Power BI) igual ao fuso IANA configurado."""
    async with engine.begin() as conn:
        rows = dict((await conn.execute(select(AppSetting.key, AppSetting.value))).all())
        wanted = windows_timezone(rows.get("display_timezone", "")) or DEFAULT_WINDOWS_TZ
        if rows.get("display_timezone_windows") != wanted:
            # [key]: KEY é palavra reservada no SQL Server; os colchetes também valem no SQLite
            await conn.execute(text("UPDATE app_setting SET value = :v WHERE [key] = 'display_timezone_windows'"), {"v": wanted})
