"""Live trading runner — real Hyperliquid execution.

Usage:
    python scripts/run_live.py           # interactive (prompts for confirmation)
    docker compose up -d trader          # containerized (set LIVE_CONFIRM=yes in .env)

Requires HL_TESTNET=false in .env for mainnet. Defaults to true (testnet).
Do NOT run this until paper trading (run_sandbox.py) has been stable for 2+ weeks.

Configure via .env or environment variables:
    STRATEGY=MACross          # MACross | …Cross | MACrossLongOnly | …CrossLongOnly | MACrossATR | MACDRSI
    INSTRUMENT_ID=BTC-USD-PERP.HYPERLIQUID
    BAR_INTERVAL=1-HOUR-LAST-EXTERNAL
    TRADE_NOTIONAL=100        # USD notional per trade (all strategies)
    HL_TESTNET=false
    HL_PRIVATE_KEY=<your-key>
    LIVE_CONFIRM=yes          # required for containerized live trading (bypasses input())

Ctrl+C (local) or `docker stop` (container) for graceful shutdown.
"""

import asyncio
import json
import signal
import sys
import types
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import asyncpg
from nautilus_trader.adapters.hyperliquid.config import (  # type: ignore[attr-defined]
    HyperliquidDataClientConfig,
    HyperliquidEnvironment,  # re-exported pyo3 enum, missing from __all__
    HyperliquidExecClientConfig,
)
from nautilus_trader.adapters.hyperliquid.factories import (
    HyperliquidLiveDataClientFactory,
    HyperliquidLiveExecClientFactory,
)
from nautilus_trader.cache.config import CacheConfig
from nautilus_trader.common.config import DatabaseConfig, LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.config import LiveExecEngineConfig, LiveRiskEngineConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.actors.alert import AlertActor, AlertActorConfig
from src.actors.persistence import PersistenceActor, PersistenceActorConfig
from src.config.settings import Settings, get_settings
from src.core import bar_type_str

RUN_MODE = "live"


def _build_strategy(
    strategy_name: str,
    instrument_id: InstrumentId,
    bar_type: BarType,
    trade_notional: Decimal,
    settings: Settings,
) -> tuple[object, str, dict[str, object]]:
    """Build the selected strategy with hyperparameters from ``settings``.

    Family-specific aliases (``EMACross``, ``SMACross``, ...) pin the MA
    family by their name; the generic ``MACross`` reads ``settings.ma_type``.
    All shared hyperparameters (fast/slow periods, ATR multipliers, MACD/RSI
    lookbacks) come from ``settings`` — see :class:`src.config.settings.Settings`.
    """
    _ma_cross_aliases = {
        "EMACross": "EMA", "SMACross": "SMA", "HMACross": "HMA",
        "DEMACross": "DEMA", "AMACross": "AMA", "VIDYACross": "VIDYA",
    }
    if strategy_name == "MACross" or strategy_name in _ma_cross_aliases:
        from src.strategies.ma_cross import MACross, MACrossConfig
        ma_type = _ma_cross_aliases.get(strategy_name, settings.ma_type)
        fast, slow = settings.ma_fast, settings.ma_slow
        stop_pct = settings.stop_pct
        bootstrap_on_deploy = settings.bootstrap_on_deploy
        return MACross(MACrossConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_notional=trade_notional,
            ma_type=ma_type,
            fast_period=fast,
            slow_period=slow,
            stop_pct=stop_pct,
            bootstrap_on_deploy=bootstrap_on_deploy,
            # Liquidation simulator is backtest-only — disable for live so
            # the LiquidationAware mixin doesn't place reduce-only stops
            # on the real venue's order book. See docs/LIQUIDATION_AND_SIZING.md.
            liquidation=None,
        )), f"MACross-{ma_type}-{fast}-{slow}", {
            "ma_type": ma_type, "fast": fast, "slow": slow,
            "notional": str(trade_notional), "stop_pct": stop_pct,
            "bootstrap_on_deploy": bootstrap_on_deploy,
        }

    _ma_cross_lo_aliases = {
        "EMACrossLongOnly": "EMA", "SMACrossLongOnly": "SMA", "HMACrossLongOnly": "HMA",
        "DEMACrossLongOnly": "DEMA", "AMACrossLongOnly": "AMA", "VIDYACrossLongOnly": "VIDYA",
    }
    if strategy_name == "MACrossLongOnly" or strategy_name in _ma_cross_lo_aliases:
        from src.strategies.ma_cross_long_only import MACrossLongOnly, MACrossLongOnlyConfig
        ma_type = _ma_cross_lo_aliases.get(strategy_name, settings.ma_type)
        fast, slow = settings.ma_fast, settings.ma_slow
        return MACrossLongOnly(MACrossLongOnlyConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_notional=trade_notional,
            ma_type=ma_type,
            fast_period=fast,
            slow_period=slow,
        )), f"MACrossLongOnly-{ma_type}-{fast}-{slow}", {
            "ma_type": ma_type, "fast": fast, "slow": slow, "notional": str(trade_notional),
        }

    if strategy_name == "MACrossATR":
        from src.strategies.ma_cross_atr import MACrossATR, MACrossATRConfig
        fast, slow = settings.ma_fast, settings.ma_slow
        atr = settings.macross_atr_period
        sl_mult, tp_mult = settings.macross_atr_sl_mult, settings.macross_atr_tp_mult
        return MACrossATR(MACrossATRConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_notional=trade_notional,
            fast_period=fast,
            slow_period=slow,
            atr_period=atr,
            atr_sl_multiplier=sl_mult,
            atr_tp_multiplier=tp_mult,
        )), f"MACrossATR-{fast}-{slow}-{atr}", {
            "fast": fast, "slow": slow, "atr": atr,
            "sl_mult": sl_mult, "tp_mult": tp_mult, "notional": str(trade_notional),
        }

    if strategy_name == "MACDRSI":
        from src.strategies.macd_rsi import MACDRSI, MACDRSIConfig
        macd_fast = settings.macdrsi_macd_fast
        macd_slow = settings.macdrsi_macd_slow
        signal_period = settings.macdrsi_macd_signal
        rsi = settings.macdrsi_rsi_period
        return MACDRSI(MACDRSIConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_notional=trade_notional,
            macd_fast_period=macd_fast,
            macd_slow_period=macd_slow,
            macd_signal_period=signal_period,
            rsi_period=rsi,
        )), f"MACDRSI-{macd_fast}-{macd_slow}-{signal_period}-{rsi}", {
            "macd_fast": macd_fast, "macd_slow": macd_slow,
            "signal": signal_period, "rsi": rsi, "notional": str(trade_notional),
        }

    valid = [
        "MACross", "EMACross", "SMACross", "HMACross",
        "DEMACross", "AMACross", "VIDYACross",
        "MACrossLongOnly", "EMACrossLongOnly", "SMACrossLongOnly", "HMACrossLongOnly",
        "DEMACrossLongOnly", "AMACrossLongOnly", "VIDYACrossLongOnly",
        "MACrossATR", "MACDRSI",
    ]
    msg = f"Unknown strategy: {strategy_name!r}. Valid: {valid}"
    raise ValueError(msg)


def main() -> None:
    # Map SIGTERM → KeyboardInterrupt so `docker stop` triggers the same
    # graceful shutdown path as Ctrl+C. Without this, SIGTERM's default
    # behavior (sys.exit → SystemExit) may not propagate reliably through
    # NT's Rust/C extensions in node.run().
    def _sigterm_handler(sig: int, frame: types.FrameType | None) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm_handler)

    settings = get_settings()

    # Safety check — require explicit mainnet opt-in
    if not settings.hl_testnet:
        if settings.live_confirm:
            print("WARNING: HL_TESTNET=false — MAINNET trading active (LIVE_CONFIRM=yes).")
        else:
            print("WARNING: HL_TESTNET=false — this will trade on MAINNET with real funds.")
            try:
                confirm = input("Type 'yes' to proceed: ")
                if confirm.strip().lower() != "yes":
                    print("Aborted.")
                    sys.exit(0)
            except EOFError:
                print("ERROR: Non-interactive environment detected. Set LIVE_CONFIRM=yes in .env to confirm mainnet trading.")
                sys.exit(1)

    if not settings.hl_private_key:
        print("ERROR: HL_PRIVATE_KEY not set in .env")
        sys.exit(1)

    run_id = str(uuid.uuid4())

    instrument_id = InstrumentId.from_str(settings.instrument_id)
    # settings.bar_interval is the friendly form ("4h"); convert to the
    # full NT bar-type string at this boundary.
    bar_type = BarType.from_str(bar_type_str(settings.instrument_id, settings.bar_interval))

    strategy, strategy_id, config_dict = _build_strategy(
        settings.strategy, instrument_id, bar_type,
        settings.trade_notional, settings,
    )

    print(f"Starting {RUN_MODE} run: {strategy_id} on {instrument_id} ({settings.bar_interval})")

    asyncio.run(_register_run(
        settings.postgres_dsn,
        run_id,
        settings.trader_id,
        strategy_id,
        str(instrument_id),
        RUN_MODE,
        config_dict,
    ))

    # Recurrence guard for orphan strategy_runs rows — mirrors run_sandbox.py.
    # Everything after `_register_run` must be wrapped so a startup crash
    # (TradingNode init, factory registration, actor add, `node.build()`)
    # still calls `_close_run()` to stamp `stopped_at`. Otherwise the
    # Grafana "Active Runs" panel undercounts forever. See run_sandbox.py
    # for the full incident context.
    node: TradingNode | None = None
    interrupted = False
    try:
        node = TradingNode(config=TradingNodeConfig(
            trader_id=settings.trader_id,
            logging=LoggingConfig(log_level="INFO"),
            # Startup reconciliation MUST stay on for live trading. Without it,
            # any restart (crash, redeploy, migration) leaves the cache empty
            # while the exchange still holds the open position + protective stop
            # — the next cross signal would double the position size and leave
            # the prior reduce-only stop orphaned. With reconciliation=True the
            # exec engine queries the venue on startup, replays order/fill/
            # position status reports through the HL adapter's generate_*
            # hooks, and rehydrates the cache to match server state before
            # the strategy receives any bars. Continuous open/position checks
            # (open_check_interval_secs, position_check_interval_secs) are
            # left off; revisit after Stage B if drift is observed.
            # NT's default is already True — set explicitly so it can't be
            # silently flipped, and so the regression test in
            # tests/unit/test_runner_kernel_paths.py has something to pin.
            exec_engine=LiveExecEngineConfig(
                reconciliation=True,
            ),
            cache=CacheConfig(
                database=DatabaseConfig(
                    host=settings.redis_host,
                    port=settings.redis_port,
                    # MUST be set; see run_sandbox.py for full rationale.
                    password=settings.redis_password,
                ),
            ),
            # Persist per-strategy user state across restarts via the
            # Redis-backed cache. See run_sandbox.py for the rationale.
            save_state=True,
            load_state=True,
            risk_engine=LiveRiskEngineConfig(
                max_order_submit_rate="100/00:00:01",
                max_order_modify_rate="100/00:00:01",
                # HL-specific: see run_sandbox.py for the USD vs USDC explanation.
                # The live HyperliquidExecClient denominates orders in USD per the
                # adapter's /info convention; the RiskEngine's max_notional_per_order
                # dict must key off the same currency.
                max_notional_per_order={"USD": 500},
            ),
            data_clients={
                "HYPERLIQUID": HyperliquidDataClientConfig(
                    environment=(
                        HyperliquidEnvironment.TESTNET
                        if settings.hl_testnet
                        else HyperliquidEnvironment.MAINNET
                    ),
                ),
            },
            exec_clients={
                "HYPERLIQUID": HyperliquidExecClientConfig(
                    private_key=settings.hl_private_key,
                    vault_address=settings.hl_wallet_address or None,
                    environment=(
                        HyperliquidEnvironment.TESTNET
                        if settings.hl_testnet
                        else HyperliquidEnvironment.MAINNET
                    ),
                ),
            },
        ))

        node.add_data_client_factory("HYPERLIQUID", HyperliquidLiveDataClientFactory)
        node.add_exec_client_factory("HYPERLIQUID", HyperliquidLiveExecClientFactory)

        node.trader.add_actor(PersistenceActor(PersistenceActorConfig(
            postgres_dsn=settings.postgres_dsn,
            run_id=run_id,
            venue="HYPERLIQUID",
            instrument_id=settings.instrument_id,
        )))
        node.trader.add_actor(AlertActor(AlertActorConfig(
            telegram_token=settings.telegram_token,
            telegram_chat_id=settings.telegram_chat_id,
            enabled=settings.telegram_enabled,
            venue="HYPERLIQUID",
        )))

        node.trader.add_strategy(strategy)

        node.build()

        # NT's kernel auto-load runs during ``__init__`` against the
        # config.strategies/actors lists.  We add strategies imperatively
        # AFTER kernel init, so we trigger ``trader.load()`` manually to
        # restore per-strategy state from Redis.  See run_sandbox.py for
        # the full rationale.
        node.trader.load()

        try:
            node.run()
        except KeyboardInterrupt:
            interrupted = True
            print("KeyboardInterrupt received, coordinating shutdown...")
    finally:
        if node is not None:
            try:
                node.stop()
            except Exception as exc:
                print(f"Warning: node.stop() raised {type(exc).__name__}: {exc}")
            try:
                node.dispose()
            except Exception as exc:
                print(f"Warning: node.dispose() raised {type(exc).__name__}: {exc}")
        # Best-effort: never let a DB cleanup error mask the original failure.
        try:
            asyncio.run(_close_run(settings.postgres_dsn, run_id))
        except Exception as exc:
            print(f"run_live: _close_run failed: {type(exc).__name__}: {exc}")

    if interrupted:
        print("Interrupted by user, shutdown complete.")


async def _register_run(
    dsn: str,
    run_id: str,
    trader_id: str,
    strategy_id: str,
    instrument_id: str,
    mode: str,
    config_dict: dict[str, object],
) -> None:
    """Insert a fresh ``strategy_runs`` row, linking to the previous run.

    See ``scripts/run_sandbox.py:_register_run`` for the full rationale —
    this is the live-runner mirror. Same lookup key
    ``(trader_id, instrument_id, strategy_id, run_mode)``, so a sandbox
    run and a live run for the same trader never link to each other
    even when the rest of the tuple matches.
    """
    conn = await asyncpg.connect(dsn)
    try:
        parent_id = await conn.fetchval(
            """
            SELECT id FROM strategy_runs
            WHERE trader_id = $1
              AND instrument_id = $2
              AND strategy_id = $3
              AND run_mode = $4
            ORDER BY started_at DESC
            LIMIT 1
            """,
            trader_id,
            instrument_id,
            strategy_id,
            mode,
        )
        await conn.execute(
            """
            INSERT INTO strategy_runs (
                id, trader_id, strategy_id, instrument_id, run_mode,
                started_at, config, parent_run_id
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            uuid.UUID(run_id),
            trader_id,
            strategy_id,
            instrument_id,
            mode,
            datetime.now(tz=UTC),
            json.dumps(config_dict),
            parent_id,
        )
    finally:
        await conn.close()


async def _close_run(dsn: str, run_id: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "UPDATE strategy_runs SET stopped_at = $1 WHERE id = $2",
            datetime.now(tz=UTC),
            uuid.UUID(run_id),
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    main()
