"""Exchange venue constants for NautilusTrader backtesting."""

from decimal import Decimal

from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.identifiers import Venue

# ── Hyperliquid ─────────────────────────────────────────────────────

HYPERLIQUID_VENUE = Venue("HYPERLIQUID")
HYPERLIQUID_API_URL = "https://api.hyperliquid.xyz/info"

# Fees — Hyperliquid base tier (VIP 0).
# Fees are tiered by 30d volume and HLP staking. Base tier used here.
# NautilusTrader 1.224.0 removed builder fee charges from the Hyperliquid
# adapter; these maker/taker fees now match live execution more closely.
MAKER_FEE = Decimal("0.00010")
TAKER_FEE = Decimal("0.00035")

# Settlement currency for all HL perps
SETTLEMENT_CURRENCY = USDC

# HL candleSnapshot API max candles per request
HL_CANDLE_LIMIT = 5000

# ── Binance ─────────────────────────────────────────────────────────

BINANCE_FUTURES_API_URL = "https://fapi.binance.com"
BINANCE_TESTNET_API_URL = "https://testnet.binancefuture.com"
BINANCE_SPOT_API_URL = "https://api.binance.com"

# Fees — Binance Futures base tier (VIP 0).
BINANCE_MAKER_FEE = Decimal("0.000200")  # 0.02%
BINANCE_TAKER_FEE = Decimal("0.000500")  # 0.05%

# Fees — Binance Spot base tier (VIP 0).
BINANCE_SPOT_MAKER_FEE = Decimal("0.001000")  # 0.10%
BINANCE_SPOT_TAKER_FEE = Decimal("0.001000")  # 0.10%

# Binance klines API max candles per request (same for futures and spot)
BINANCE_CANDLE_LIMIT = 1500

# Map HL interval strings to NT BarSpec components: (step, aggregation_string)
# Used to build BarType strings like "{instrument_id}-{step}-{agg}-LAST-EXTERNAL"
INTERVAL_TO_BAR_SPEC: dict[str, tuple[int, str]] = {
    "1m": (1, "MINUTE"),
    "5m": (5, "MINUTE"),
    "15m": (15, "MINUTE"),
    "1h": (1, "HOUR"),
    "4h": (4, "HOUR"),
    "1d": (1, "DAY"),
}

# ts_init_delta per interval in nanoseconds.
# BarDataWrangler.process(ts_init_delta=int) shifts ts_init forward from ts_event.
# HL candleSnapshot returns bar-OPEN timestamps. Without this shift, the strategy
# sees the complete bar at bar-open time — classic look-ahead bias.
_NS_PER_MINUTE = 60_000_000_000
_NS_PER_HOUR = 3_600_000_000_000
_NS_PER_DAY = 86_400_000_000_000

TS_INIT_DELTAS: dict[str, int] = {
    "1m": 1 * _NS_PER_MINUTE,
    "5m": 5 * _NS_PER_MINUTE,
    "15m": 15 * _NS_PER_MINUTE,
    "1h": 1 * _NS_PER_HOUR,
    "4h": 4 * _NS_PER_HOUR,
    "1d": 1 * _NS_PER_DAY,
}
