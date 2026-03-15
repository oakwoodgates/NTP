"""Paper trading runner — live Hyperliquid data, simulated execution.

Usage:
    python scripts/run_sandbox.py

Configure via .env or environment variables:
    STRATEGY=EMACross         # EMACross | SMACross | EMACrossATR | MACDRSI
    INSTRUMENT_ID=BTC-USD-PERP.HYPERLIQUID
    BAR_INTERVAL=1-HOUR-LAST-EXTERNAL
    TRADE_NOTIONAL=100        # USD notional per trade (all strategies)
    STARTING_BALANCE=10000

Ctrl+C (local) or `docker stop` (container) for graceful shutdown.
"""

import asyncio
import json
import signal
import types
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import asyncpg
from nautilus_trader.adapters.hyperliquid.config import HyperliquidDataClientConfig
from nautilus_trader.adapters.hyperliquid.factories import HyperliquidLiveDataClientFactory
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
from nautilus_trader.cache.config import CacheConfig
from nautilus_trader.common.config import DatabaseConfig, InstrumentProviderConfig, LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.config import LiveExecEngineConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.actors.alert import AlertActor, AlertActorConfig
from src.actors.persistence import PersistenceActor, PersistenceActorConfig
from src.config.settings import get_settings

RUN_MODE = "sandbox"


def _build_strategy(
    strategy_name: str,
    instrument_id: InstrumentId,
    bar_type: BarType,
    trade_notional: Decimal,
) -> tuple[object, str, dict[str, object]]:
    """Build the selected strategy with default parameters.

    To customize strategy-specific parameters, edit the defaults below.
    """
    if strategy_name == "EMACross":
        from src.strategies.ema_cross import EMACross, EMACrossConfig
        fast, slow = 5, 45
        return EMACross(EMACrossConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_notional=trade_notional,
            fast_ema_period=fast,
            slow_ema_period=slow,
        )), f"EMACross-{fast}-{slow}", {"fast": fast, "slow": slow, "notional": str(trade_notional)}

    if strategy_name == "SMACross":
        from src.strategies.sma_cross import SMACross, SMACrossConfig
        fast, slow = 10, 20
        return SMACross(SMACrossConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_notional=trade_notional,
            fast_sma_period=fast,
            slow_sma_period=slow,
        )), f"SMACross-{fast}-{slow}", {"fast": fast, "slow": slow, "notional": str(trade_notional)}

    if strategy_name == "EMACrossATR":
        from src.strategies.ema_cross_atr import EMACrossATR, EMACrossATRConfig
        fast, slow, atr = 20, 50, 14
        sl_mult, tp_mult = 1.5, 3.0
        return EMACrossATR(EMACrossATRConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_notional=trade_notional,
            fast_ema_period=fast,
            slow_ema_period=slow,
            atr_period=atr,
            atr_sl_multiplier=sl_mult,
            atr_tp_multiplier=tp_mult,
        )), f"EMACrossATR-{fast}-{slow}-{atr}", {
            "fast": fast, "slow": slow, "atr": atr,
            "sl_mult": sl_mult, "tp_mult": tp_mult, "notional": str(trade_notional),
        }

    if strategy_name == "MACDRSI":
        from src.strategies.macd_rsi import MACDRSI, MACDRSIConfig
        macd_fast, macd_slow, signal, rsi = 12, 26, 9, 14
        return MACDRSI(MACDRSIConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_notional=trade_notional,
            macd_fast_period=macd_fast,
            macd_slow_period=macd_slow,
            macd_signal_period=signal,
            rsi_period=rsi,
        )), f"MACDRSI-{macd_fast}-{macd_slow}-{signal}-{rsi}", {
            "macd_fast": macd_fast, "macd_slow": macd_slow,
            "signal": signal, "rsi": rsi, "notional": str(trade_notional),
        }

    valid = ["EMACross", "SMACross", "EMACrossATR", "MACDRSI"]
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
    run_id = str(uuid.uuid4())
    instrument_id_str = settings.instrument_id
    instrument_id = InstrumentId.from_str(instrument_id_str)
    bar_interval = settings.bar_interval
    bar_type = BarType.from_str(f"{instrument_id}-{bar_interval}")

    strategy, strategy_id, config_dict = _build_strategy(
        settings.strategy, instrument_id, bar_type,
        Decimal(settings.trade_notional),
    )

    print(f"Starting {RUN_MODE} run: {strategy_id} on {instrument_id} ({bar_interval})")

    # Register run in DB before node starts
    asyncio.run(_register_run(
        settings.postgres_dsn,
        run_id,
        settings.trader_id,
        strategy_id,
        instrument_id_str,
        RUN_MODE,
        config_dict,
    ))

    node = TradingNode(config=TradingNodeConfig(
        trader_id=settings.trader_id,
        logging=LoggingConfig(log_level="INFO"),
        exec_engine=LiveExecEngineConfig(
            reconciliation=False,
        ),
        cache=CacheConfig(
            database=DatabaseConfig(
                host=settings.redis_host,
                port=settings.redis_port,
            ),
        ),
        data_clients={
            "HYPERLIQUID": HyperliquidDataClientConfig(
                instrument_provider=InstrumentProviderConfig(
                    load_ids=frozenset((instrument_id,)),
                ),
                testnet=False,  # Real market data; simulated execution via Sandbox
            ),
        },
        exec_clients={
            "HYPERLIQUID": SandboxExecutionClientConfig(
                venue="HYPERLIQUID",
                starting_balances=[f"{settings.starting_balance} USDC"],
            ),
        },
    ))

    # Register adapter factories
    node.add_data_client_factory("HYPERLIQUID", HyperliquidLiveDataClientFactory)
    node.add_exec_client_factory("HYPERLIQUID", SandboxLiveExecClientFactory)

    # Add actors
    node.trader.add_actor(PersistenceActor(PersistenceActorConfig(
        postgres_dsn=settings.postgres_dsn,
        run_id=run_id,
        venue="HYPERLIQUID",
        instrument_id=instrument_id_str,
    )))
    node.trader.add_actor(AlertActor(AlertActorConfig(
        telegram_token=settings.telegram_token,
        telegram_chat_id=settings.telegram_chat_id,
        enabled=settings.telegram_enabled,
        venue="HYPERLIQUID",
        instrument_id=instrument_id_str,
    )))

    # Add strategy
    node.trader.add_strategy(strategy)

    node.build()
    interrupted = False
    try:
        node.run()
    except KeyboardInterrupt:
        interrupted = True
    except Exception as exc:
        print(f"run_sandbox: node.run() raised {type(exc).__name__}: {exc}")
        raise
    finally:
        try:
            node.stop()
        except Exception as exc:
            print(f"run_sandbox: node.stop() raised {type(exc).__name__}: {exc}")
        node.dispose()
        asyncio.run(_close_run(settings.postgres_dsn, run_id))
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
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """
            INSERT INTO strategy_runs (
                id, trader_id, strategy_id, instrument_id, run_mode, started_at, config
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            uuid.UUID(run_id),
            trader_id,
            strategy_id,
            instrument_id,
            mode,
            datetime.now(tz=UTC),
            json.dumps(config_dict),
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
