"""Pydantic Settings — single source of truth for all environment configuration.

This is the canonical config object for the WHOLE system: backtest
notebooks, batch runner, sandbox runner, live runner, validate,
compare-sweeps — everyone reads from ``get_settings()``.  The same
.env file flows from research → paper → live, so a value validated
in backtest deploys with the same number, no manual re-entry.

Override per environment via ``.env``::

    STARTING_CAPITAL=1000
    TRADE_NOTIONAL=2000
    LEVERAGE=20
    BAR_INTERVAL=4h
    DEFAULT_STOP_PCT=0.05

For one-off experiments (different capital, different leverage), set
the env var or override the imported value locally in cell 1::

    settings = get_settings()
    STARTING_CAPITAL = 500   # one-off override; settings.starting_capital still 1000
"""

from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore",
    )

    # ── Database ────────────────────────────────────────────────────────
    postgres_host: str = "localhost"
    postgres_port: int = 5434
    postgres_db: str = "nautilus_platform"
    postgres_user: str = "nautilus"
    postgres_password: str = ""

    # ── Redis ───────────────────────────────────────────────────────────
    redis_host: str = "localhost"
    redis_port: int = 6379

    # ── Telegram ────────────────────────────────────────────────────────
    telegram_token: str = ""
    telegram_chat_id: str = ""

    # ── Hyperliquid ─────────────────────────────────────────────────────
    hl_wallet_address: str = ""
    hl_private_key: str = ""
    hl_testnet: bool = True  # Safety default — must explicitly set False for mainnet
    live_confirm: bool = False  # LIVE_CONFIRM=yes to bypass interactive prompt

    # ── Account / trader ────────────────────────────────────────────────
    # The "single trader" config that flows through backtest, sandbox,
    # and live.  Override each field via .env per deployment if needed.
    trader_id: str = "TRADER-001"

    starting_capital: int = 1000
    """USD seed capital. Used by backtest engines, sandbox SimulatedExchange
    (``starting_balances``), and live monitoring (alive-floor calculations)."""

    trade_notional: Decimal = Decimal("2000")
    """USD notional per trade. With ``leverage=20`` and a 5% protective stop
    ($100 risk), this is the project's standard 'aggressive but bounded'
    setup — worst-case loss per trade equals the initial margin (isolated-
    margin equivalence under cross-margin accounting)."""

    leverage: int = 20
    """Account leverage applied to the engine.  Combined with
    ``trade_notional`` above, defines gross leverage on the position."""

    # ── Default venue + data ────────────────────────────────────────────
    data_source: str = "BINANCE_PERP"
    """Where catalog data comes from. Binance has the deepest history;
    Hyperliquid for native HL data when available."""

    exec_venue: str = "HYPERLIQUID_PERP"
    """Where execution is simulated / routed live."""

    # ── Strategy + bar interval ─────────────────────────────────────────
    strategy: str = "MACross"
    """Strategy identifier used by sandbox/live runners. Backtest notebooks
    pick their own (one notebook per strategy)."""

    instrument_id: str = "BTC-USD-PERP.HYPERLIQUID"
    """Default instrument for sandbox/live. Backtest notebooks override
    via ``ASSET`` + ``DATA_SOURCE`` in cell 1."""

    bar_interval: str = "4h"
    """Friendly bar-interval string (e.g. ``"1d"``, ``"4h"``, ``"1h"``,
    ``"15m"``). Use ``src.core.utils.bar_type_str(instrument_id, interval)``
    to produce the full NT bar-type string at the boundary. NT internals
    store the suffix form (``"4-HOUR-LAST-EXTERNAL"``); we stay friendly
    everywhere user-facing."""

    # ── Risk management ─────────────────────────────────────────────────
    default_stop_pct: float | None = 0.05
    """Default protective-stop fraction (``0.05`` = 5%). At
    ``stop_pct = 1/leverage`` (20× → 0.05) worst-case loss per trade
    equals the initial margin committed. Set to ``None`` to disable
    the protective-stop mixin by default."""

    # ── Strategy hyperparameters ────────────────────────────────────────
    # Pulled out of the runners so swapping fast/slow / ATR multipliers /
    # MACD-RSI periods doesn't require editing Python source and rebuilding
    # the trader image. Backtest notebooks pick their own (one notebook per
    # strategy); these defaults apply to sandbox/live runners.
    #
    # NOTE: flat namespace will grow as strategies multiply. If it gets
    # unwieldy, switch to a single JSON-encoded STRATEGY_PARAMS env var.

    # — MA crossover family (MACross, MACrossLongOnly, *Cross specialisations)
    ma_fast: int = 10
    """Fast MA period. Default 10 matches the canonical backtest combo
    used in ``notebooks/backtest/ma_cross.ipynb``."""

    ma_slow: int = 100
    """Slow MA period. Default 100 was data-driven — picked in
    ``docs/SANDBOX_CONFIG_DECISION.md`` after a BTC/ETH/SOL × 1h/4h
    sweep over 2024-05 to 2026-03 (the only combo profitable across
    all three instruments with PF ≥ 1.3 and ≥30 trades/year)."""

    ma_type: str = "EMA"
    """MA family for MACross-style strategies. One of EMA/SMA/HMA/DEMA/AMA/VIDYA.
    Sandbox/live runners use this when ``strategy`` is the generic ``MACross``;
    the family-specific aliases (``EMACross``, ``SMACross``, ...) override this."""

    # — MACrossATR
    macross_atr_period: int = 14
    """ATR lookback for MACrossATR strategy."""

    macross_atr_sl_mult: float = 1.5
    """ATR multiplier for the stop-loss leg of MACrossATR's bracket. Stored
    as float because NT's ``MACrossATRConfig`` expects float (multiplier,
    not a money value — no precision concern)."""

    macross_atr_tp_mult: float = 3.0
    """ATR multiplier for the take-profit leg of MACrossATR's bracket. Stored
    as float because NT's ``MACrossATRConfig`` expects float."""

    # — MACDRSI
    macdrsi_macd_fast: int = 12
    """Fast EMA period in the MACD oscillator."""

    macdrsi_macd_slow: int = 26
    """Slow EMA period in the MACD oscillator."""

    macdrsi_macd_signal: int = 9
    """Signal-line EMA period in the MACD oscillator."""

    macdrsi_rsi_period: int = 14
    """RSI lookback in the MACDRSI strategy."""

    # ── Liquidation simulator ───────────────────────────────────────────
    liquidation_enabled: bool = True
    """Wire up the LiquidationAware mixin + AccountAliveMonitor actor.
    Set False for live (the venue handles its own liquidation)."""

    liquidation_min_trade_notional: Decimal = Decimal("10")
    """Floor used by AccountAliveMonitor to decide 'can the account still
    afford a trade'. Smaller than any sensible position; not the binding
    constraint in practice."""

    # ── Default test universe (used by batch runner + research) ─────────
    default_assets: list[str] = ["BTC", "ETH", "SOL"]
    """Standard asset list for batch backtests + sweep comparisons."""

    default_intervals: list[str] = ["4h", "1d"]
    """Standard interval list for batch backtests + sweep comparisons."""

    # ── Derived properties ──────────────────────────────────────────────
    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    # ── Back-compat alias ───────────────────────────────────────────────
    @property
    def starting_balance(self) -> int:
        """Deprecated alias for ``starting_capital``. Old runner code reads
        this name; new code should use ``starting_capital`` directly."""
        return self.starting_capital


@lru_cache
def get_settings() -> Settings:
    return Settings()
