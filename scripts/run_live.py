"""Live trading runner — real Hyperliquid execution.

Usage:
    python scripts/run_live.py           # interactive (prompts for confirmation)
    docker compose up -d trader          # containerized (set LIVE_CONFIRM=yes in .env)

Requires HL_TESTNET=false in .env for mainnet. Defaults to true (testnet).
Do NOT run this until paper trading (run_sandbox.py) has been stable for 2+ weeks.

Configure via .env or environment variables:
    STRATEGY=MACross          # MACross | …Cross | MACrossLongOnly | …CrossLongOnly | EMACrossATR | MACDRSI
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
from nautilus_trader.adapters.hyperliquid.config import (
    HyperliquidDataClientConfig,
    HyperliquidExecClientConfig,
)
from nautilus_trader.adapters.hyperliquid.factories import (
    HyperliquidLiveDataClientFactory,
    HyperliquidLiveExecClientFactory,
)
from nautilus_trader.cache.config import CacheConfig
from nautilus_trader.common.config import DatabaseConfig, LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.config import LiveRiskEngineConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from src.actors.alert import AlertActor, AlertActorConfig
from src.actors.persistence import PersistenceActor, PersistenceActorConfig
from src.config.settings import get_settings

RUN_MODE = "live"


def _build_strategy(
    strategy_name: str,
    instrument_id: InstrumentId,
    bar_type: BarType,
    trade_notional: Decimal,
) -> tuple[object, str, dict[str, object]]:
    """Build the selected strategy with default parameters.

    To customize strategy-specific parameters, edit the defaults below.
    """
    _ma_cross_types = {
        "MACross": "EMA", "EMACross": "EMA", "SMACross": "SMA", "HMACross": "HMA",
        "DEMACross": "DEMA", "AMACross": "AMA", "VIDYACross": "VIDYA",
    }
    if strategy_name in _ma_cross_types:
        from src.strategies.ma_cross import MACross, MACrossConfig
        ma_type = _ma_cross_types[strategy_name]
        fast, slow = 10, 20
        return MACross(MACrossConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_notional=trade_notional,
            ma_type=ma_type,
            fast_period=fast,
            slow_period=slow,
        )), f"MACross-{ma_type}-{fast}-{slow}", {
            "ma_type": ma_type, "fast": fast, "slow": slow, "notional": str(trade_notional),
        }

    _ma_cross_lo_types = {
        "MACrossLongOnly": "EMA", "EMACrossLongOnly": "EMA",
        "SMACrossLongOnly": "SMA", "HMACrossLongOnly": "HMA",
        "DEMACrossLongOnly": "DEMA", "AMACrossLongOnly": "AMA",
        "VIDYACrossLongOnly": "VIDYA",
    }
    if strategy_name in _ma_cross_lo_types:
        from src.strategies.ma_cross_long_only import MACrossLongOnly, MACrossLongOnlyConfig
        ma_type = _ma_cross_lo_types[strategy_name]
        fast, slow = 10, 20
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

    valid = [
        "MACross", "EMACross", "SMACross", "HMACross",
        "DEMACross", "AMACross", "VIDYACross",
        "MACrossLongOnly", "EMACrossLongOnly", "SMACrossLongOnly", "HMACrossLongOnly",
        "DEMACrossLongOnly", "AMACrossLongOnly", "VIDYACrossLongOnly",
        "EMACrossATR", "MACDRSI",
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
    bar_interval = settings.bar_interval
    bar_type = BarType.from_str(f"{instrument_id}-{bar_interval}")

    strategy, strategy_id, config_dict = _build_strategy(
        settings.strategy, instrument_id, bar_type,
        Decimal(settings.trade_notional),
    )

    print(f"Starting {RUN_MODE} run: {strategy_id} on {instrument_id} ({bar_interval})")

    asyncio.run(_register_run(
        settings.postgres_dsn,
        run_id,
        settings.trader_id,
        strategy_id,
        str(instrument_id),
        RUN_MODE,
        config_dict,
    ))

    node = TradingNode(config=TradingNodeConfig(
        trader_id=settings.trader_id,
        logging=LoggingConfig(log_level="INFO"),
        cache=CacheConfig(
            database=DatabaseConfig(
                host=settings.redis_host,
                port=settings.redis_port,
            ),
        ),
        risk_engine=LiveRiskEngineConfig(
            max_order_submit_rate="100/00:00:01",
            max_order_modify_rate="100/00:00:01",
            max_notional_per_order={"USDC": 500},
        ),
        data_clients={
            "HYPERLIQUID": HyperliquidDataClientConfig(
                testnet=settings.hl_testnet,
            ),
        },
        exec_clients={
            "HYPERLIQUID": HyperliquidExecClientConfig(
                private_key=settings.hl_private_key,
                vault_address=settings.hl_wallet_address or None,
                testnet=settings.hl_testnet,
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
    interrupted = False
    try:
        node.run()
    except KeyboardInterrupt:
        interrupted = True
        print("KeyboardInterrupt received, coordinating shutdown...")
    finally:
        try:
            node.stop()
        except Exception as exc:
            print(f"Warning: node.stop() raised {type(exc).__name__}: {exc}")
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
