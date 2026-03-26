"""Tests for actor config classes."""

from src.actors.alert import AlertActorConfig
from src.actors.persistence import PersistenceActorConfig


class TestPersistenceActorConfig:
    def test_create_config(self) -> None:
        config = PersistenceActorConfig(
            postgres_dsn="postgresql://localhost/test",
            run_id="00000000-0000-0000-0000-000000000000",
            instrument_id="BTC-USDT.HYPERLIQUID",
        )
        assert config.postgres_dsn == "postgresql://localhost/test"
        assert config.run_id == "00000000-0000-0000-0000-000000000000"

    def test_defaults(self) -> None:
        config = PersistenceActorConfig(
            postgres_dsn="postgresql://localhost/test",
            run_id="00000000-0000-0000-0000-000000000000",
            instrument_id="BTC-USDT.HYPERLIQUID",
        )
        assert config.venue == "HYPERLIQUID"
        assert config.snapshot_interval_secs == 60

    def test_custom_venue(self) -> None:
        config = PersistenceActorConfig(
            postgres_dsn="postgresql://localhost/test",
            run_id="00000000-0000-0000-0000-000000000000",
            instrument_id="BTC-USDT.HYPERLIQUID",
            venue="BINANCE",
        )
        assert config.venue == "BINANCE"


class TestAlertActorConfig:
    def test_create_config(self) -> None:
        config = AlertActorConfig()
        assert config.telegram_token == ""
        assert config.telegram_chat_id == ""
        assert config.enabled is False

    def test_enabled_config(self) -> None:
        config = AlertActorConfig(
            telegram_token="tok",
            telegram_chat_id="123",
            enabled=True,
        )
        assert config.enabled is True

    def test_drawdown_default(self) -> None:
        config = AlertActorConfig()
        assert config.drawdown_alert_pct == "10"

    def test_custom_drawdown(self) -> None:
        config = AlertActorConfig(drawdown_alert_pct="5")
        assert config.drawdown_alert_pct == "5"
