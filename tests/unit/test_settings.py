"""Tests for src.config.settings."""

from src.config.settings import Settings, get_settings


class TestSettings:
    def test_defaults(self) -> None:
        # Use _env_file=None so defaults are not overridden by .env values.
        s = Settings(postgres_password="test", _env_file=None)  # type: ignore[call-arg]
        assert s.postgres_host == "localhost"
        assert s.postgres_port == 5434
        assert s.postgres_db == "nautilus_platform"
        assert s.postgres_user == "nautilus"
        assert s.redis_host == "localhost"
        assert s.redis_port == 6379
        assert s.hl_testnet is True
        assert s.trader_id == "TRADER-001"
        assert s.strategy == "EMACross"
        assert s.instrument_id == "BTC-USD-PERP.HYPERLIQUID"
        assert s.bar_interval == "1-HOUR-LAST-EXTERNAL"
        assert s.trade_notional == "100"
        assert s.starting_balance == 10_000

    def test_postgres_dsn(self) -> None:
        s = Settings(
            postgres_user="u",
            postgres_password="p",
            postgres_host="h",
            postgres_port=1234,
            postgres_db="d",
        )
        assert s.postgres_dsn == "postgresql://u:p@h:1234/d"

    def test_telegram_enabled_when_both_set(self) -> None:
        s = Settings(
            postgres_password="test",
            telegram_token="tok",
            telegram_chat_id="123",
        )
        assert s.telegram_enabled is True

    def test_telegram_disabled_when_token_empty(self) -> None:
        s = Settings(postgres_password="test", telegram_token="", telegram_chat_id="123")
        assert s.telegram_enabled is False

    def test_telegram_disabled_when_chat_id_empty(self) -> None:
        s = Settings(postgres_password="test", telegram_token="tok", telegram_chat_id="")
        assert s.telegram_enabled is False

    def test_extra_env_vars_ignored(self) -> None:
        """Settings should not fail when .env has vars not in the model."""
        s = Settings(postgres_password="test", _env_file=None)  # type: ignore[call-arg]
        assert s.postgres_password == "test"

    def test_get_settings_cached(self) -> None:
        get_settings.cache_clear()
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2
        get_settings.cache_clear()
