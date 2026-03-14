"""Instrument factories for exchange perpetuals (Hyperliquid, Binance)."""

from decimal import Decimal

from nautilus_trader.adapters.binance.common.constants import BINANCE_VENUE
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Currency, Price, Quantity

from src.core.constants import (
    BINANCE_MAKER_FEE,
    BINANCE_TAKER_FEE,
    HYPERLIQUID_VENUE,
    MAKER_FEE,
    SETTLEMENT_CURRENCY,
    TAKER_FEE,
)


def make_hyperliquid_perp(
    coin: str,
    price_precision: int,
    size_precision: int,
    max_leverage: int,
    maker_fee: Decimal = MAKER_FEE,
    taker_fee: Decimal = TAKER_FEE,
) -> CryptoPerpetual:
    """Create a CryptoPerpetual instrument matching the HL adapter format.

    Parameters
    ----------
    coin : str
        The coin ticker (e.g., "BTC", "ETH", "SOL").
    price_precision : int
        Decimal places for price (e.g., 1 for BTC → tick size 0.1).
    size_precision : int
        Decimal places for size / szDecimals (e.g., 4 for BTC → 0.0001).
    max_leverage : int
        Maximum leverage (e.g., 50 for BTC).
    maker_fee : Decimal
        Maker fee rate. Default: HL VIP 0 base tier.
    taker_fee : Decimal
        Taker fee rate. Default: HL VIP 0 base tier.

    Returns
    -------
    CryptoPerpetual

    Default instrument metadata (from Hyperliquid, as of 2026-03-03):

    | Coin | price_precision | size_precision (szDecimals) | maxLeverage |
    |------|-----------------|----------------------------|-------------|
    | BTC  | 1               | 5                          | 40          |
    | ETH  | 2               | 4                          | 25          |
    | SOL  | 3               | 2                          | 20          |

    """
    margin_init = Decimal(1) / Decimal(max_leverage)
    margin_maint = margin_init / 2

    price_increment_str, size_increment_str = _precision_to_increments(
        price_precision, size_precision,
    )

    return CryptoPerpetual(
        instrument_id=InstrumentId(Symbol(f"{coin}-USD-PERP"), HYPERLIQUID_VENUE),
        raw_symbol=Symbol(coin),
        base_currency=Currency.from_str(coin),
        quote_currency=SETTLEMENT_CURRENCY,  # HL quotes in USD but settles in USDC; use USDC so commissions deduct correctly
        settlement_currency=SETTLEMENT_CURRENCY,
        is_inverse=False,
        price_precision=price_precision,
        size_precision=size_precision,
        price_increment=Price.from_str(price_increment_str),
        size_increment=Quantity.from_str(size_increment_str),
        ts_event=0,
        ts_init=0,
        margin_init=margin_init,
        margin_maint=margin_maint,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
    )


def make_binance_perp(
    coin: str,
    price_precision: int,
    size_precision: int,
    maker_fee: Decimal = BINANCE_MAKER_FEE,
    taker_fee: Decimal = BINANCE_TAKER_FEE,
) -> CryptoPerpetual:
    """Create a CryptoPerpetual instrument for Binance USDM Futures.

    Parameters
    ----------
    coin : str
        The coin ticker (e.g., "BTC", "ETH", "SOL").
    price_precision : int
        Decimal places for price (e.g., 2 for BTC → tick size 0.01).
    size_precision : int
        Decimal places for size (e.g., 3 for BTC → 0.001).
    maker_fee : Decimal
        Maker fee rate. Default: Binance VIP 0 base tier.
    taker_fee : Decimal
        Taker fee rate. Default: Binance VIP 0 base tier.

    Returns
    -------
    CryptoPerpetual

    """
    symbol = f"{coin}USDT"
    quote = Currency.from_str("USDT")

    price_increment_str, size_increment_str = _precision_to_increments(
        price_precision, size_precision,
    )

    return CryptoPerpetual(
        instrument_id=InstrumentId(Symbol(f"{symbol}-PERP"), BINANCE_VENUE),
        raw_symbol=Symbol(symbol),
        base_currency=Currency.from_str(coin),
        quote_currency=quote,
        settlement_currency=quote,
        is_inverse=False,
        price_precision=price_precision,
        size_precision=size_precision,
        price_increment=Price.from_str(price_increment_str),
        size_increment=Quantity.from_str(size_increment_str),
        ts_event=0,
        ts_init=0,
        margin_init=Decimal(1),    # Per NT Binance adapter convention
        margin_maint=Decimal(1),
        maker_fee=maker_fee,
        taker_fee=taker_fee,
    )


def _precision_to_increments(price_precision: int, size_precision: int) -> tuple[str, str]:
    """Convert precision integers to increment strings.

    precision 0 → "1", precision 1 → "0.1", precision 2 → "0.01", etc.
    """
    price_increment = "1" if price_precision == 0 else "0." + "0" * (price_precision - 1) + "1"
    size_increment = "1" if size_precision == 0 else "0." + "0" * (size_precision - 1) + "1"
    return price_increment, size_increment
