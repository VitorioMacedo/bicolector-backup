"""Configuração da aplicação (variáveis de ambiente / .env)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # SQL Server via ODBC Driver 18 (aioodbc). Instância nomeada: "host\\instancia"; porta: "host:1433".
    database_url: str = (
        "mssql+aioodbc://bic:BicDev2026x@localhost:1433/bic"
        "?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
    )
    # Login do painel. ADMIN_PASSWORD_HASH (Argon2id, gere com `uv run python -m app.auth.hashpw`) tem
    # precedência sobre ADMIN_PASSWORD; a senha em texto puro continua aceita, mas gera aviso no log.
    admin_user: str = "admin"
    admin_password: str = "admin"
    admin_password_hash: str = ""
    api_key: str = "dev-key"

    # Sessão do painel (cookie assinado). SECRET_KEY vazio: gerado a cada subida (sessões caem no restart).
    secret_key: str = ""
    session_hours: int = 8
    session_remember_days: int = 30
    cookie_secure: bool = False  # ligue quando o painel estiver atrás de HTTPS
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15

    poller_enabled: bool = True
    # Deve ser >= ao timeout serial configurado no Link150; menor que isso, a resposta atrasada de um
    # medidor ausente chega fora de ordem. Um medidor offline custa este tempo até entrar em backoff.
    modbus_timeout_s: float = 1.0
    modbus_retries: int = 0
    config_refresh_s: float = 2.0
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
