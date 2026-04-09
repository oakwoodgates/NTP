"""Pydantic Settings — single source of truth for all environment configuration."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore",
    )

    # Database
    postgres_host: str = "localhost"
    postgres_port: int = 5434
    postgres_db: str = "nautilus_platform"
    postgres_user: str = "nautilus"
    postgres_password: str = ""

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379

    # Telegram
    telegram_token: str = ""
    telegram_chat_id: str = ""

    # Hyperliquid
    hl_wallet_address: str = ""
    hl_private_key: str = ""
    hl_testnet: bool = True  # Safety default — must explicitly set False for mainnet
    live_confirm: bool = False  # Set LIVE_CONFIRM=yes in .env for containerized live trading

    # Trading
    trader_id: str = "TRADER-001"
    strategy: str = "MACross"  # MACross | …Cross | MACrossLongOnly | …CrossLongOnly | MACrossATR | MACDRSI
    instrument_id: str = "BTC-USD-PERP.HYPERLIQUID"
    bar_interval: str = "1-HOUR-LAST-EXTERNAL"
    trade_notional: str = "100"  # USD notional per trade
    starting_balance: int = 10_000

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
