"""Instrument factories for exchange instruments (Hyperliquid, Binance)."""

from decimal import Decimal

from nautilus_trader.adapters.binance.common.constants import BINANCE_VENUE
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.instruments import CryptoPerpetual, CurrencyPair
from nautilus_trader.model.objects import Currency, Price, Quantity

from src.core.constants import (
    BINANCE_MAKER_FEE,
    BINANCE_SPOT_MAKER_FEE,
    BINANCE_SPOT_TAKER_FEE,
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
    tick_size: str,
    step_size: str,
    maker_fee: Decimal = BINANCE_MAKER_FEE,
    taker_fee: Decimal = BINANCE_TAKER_FEE,
) -> CryptoPerpetual:
    """Create a CryptoPerpetual instrument for Binance USDM Futures.

    Precision and increment are derived from tick_size/step_size strings,
    matching how NT's own BinanceFuturesInstrumentProvider parses exchangeInfo.

    Parameters
    ----------
    coin : str
        The coin ticker (e.g., "BTC", "ETH", "SOL").
    tick_size : str
        Price tick size from PRICE_FILTER (e.g., "0.10" for BTC).
    step_size : str
        Order size step from LOT_SIZE (e.g., "0.001" for BTC).
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

    # Derive precision from tick/step strings — matches NT adapter (providers.py:384-387)
    price_precision = abs(int(Decimal(tick_size).as_tuple().exponent))
    size_precision = abs(int(Decimal(step_size).as_tuple().exponent))

    return CryptoPerpetual(
        instrument_id=InstrumentId(Symbol(f"{symbol}-PERP"), BINANCE_VENUE),
        raw_symbol=Symbol(symbol),
        base_currency=Currency.from_str(coin),
        quote_currency=quote,
        settlement_currency=quote,
        is_inverse=False,
        price_precision=price_precision,
        size_precision=size_precision,
        price_increment=Price.from_str(tick_size),
        size_increment=Quantity.from_str(step_size),
        ts_event=0,
        ts_init=0,
        margin_init=Decimal(1),    # Per NT Binance adapter convention
        margin_maint=Decimal(1),
        maker_fee=maker_fee,
        taker_fee=taker_fee,
    )


def make_binance_spot(
    coin: str,
    tick_size: str,
    step_size: str,
    maker_fee: Decimal = BINANCE_SPOT_MAKER_FEE,
    taker_fee: Decimal = BINANCE_SPOT_TAKER_FEE,
) -> CurrencyPair:
    """Create a CurrencyPair instrument for Binance Spot.

    Parameters
    ----------
    coin : str
        The coin ticker (e.g., "BTC", "ETH", "SOL").
    tick_size : str
        Price tick size from PRICE_FILTER (e.g., "0.01" for BTC).
    step_size : str
        Order size step from LOT_SIZE (e.g., "0.00001" for BTC).
    maker_fee : Decimal
        Maker fee rate. Default: Binance Spot VIP 0 base tier.
    taker_fee : Decimal
        Taker fee rate. Default: Binance Spot VIP 0 base tier.

    Returns
    -------
    CurrencyPair

    """
    symbol = f"{coin}USDT"
    quote = Currency.from_str("USDT")

    price_precision = abs(int(Decimal(tick_size).as_tuple().exponent))
    size_precision = abs(int(Decimal(step_size).as_tuple().exponent))

    return CurrencyPair(
        instrument_id=InstrumentId(Symbol(symbol), BINANCE_VENUE),
        raw_symbol=Symbol(symbol),
        base_currency=Currency.from_str(coin),
        quote_currency=quote,
        price_precision=price_precision,
        size_precision=size_precision,
        price_increment=Price.from_str(tick_size),
        size_increment=Quantity.from_str(step_size),
        ts_event=0,
        ts_init=0,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
    )


def with_venue_config(
    instrument: CryptoPerpetual,
    max_leverage: int,
    maker_fee: Decimal | None = None,
    taker_fee: Decimal | None = None,
) -> CryptoPerpetual:
    """Clone a CryptoPerpetual with venue-specific backtesting overrides.

    Catalog instruments store raw margins and default exchange fees. For
    backtesting, NT's risk engine enforces margin values, and fees affect PnL
    calculations. This function clones the instrument with:
    - margin_init = 1/max_leverage, margin_maint = margin_init/2
    - Optional maker/taker fee overrides (when None, preserves catalog fees)

    Works for any exchange — no factory-specific logic.

    Parameters
    ----------
    instrument : CryptoPerpetual
        The source instrument (typically from ParquetDataCatalog).
    max_leverage : int
        Desired leverage. Margin is derived as 1/max_leverage.
    maker_fee : Decimal | None
        Override maker fee. None preserves the instrument's existing fee.
    taker_fee : Decimal | None
        Override taker fee. None preserves the instrument's existing fee.

    Returns
    -------
    CryptoPerpetual

    """
    margin_init = Decimal(1) / Decimal(max_leverage)
    margin_maint = margin_init / 2

    return CryptoPerpetual(
        instrument_id=instrument.id,
        raw_symbol=instrument.raw_symbol,
        base_currency=instrument.base_currency,
        quote_currency=instrument.quote_currency,
        settlement_currency=instrument.settlement_currency,
        is_inverse=instrument.is_inverse,
        price_precision=instrument.price_precision,
        size_precision=instrument.size_precision,
        price_increment=instrument.price_increment,
        size_increment=instrument.size_increment,
        ts_event=instrument.ts_event,
        ts_init=instrument.ts_init,
        margin_init=margin_init,
        margin_maint=margin_maint,
        maker_fee=maker_fee if maker_fee is not None else instrument.maker_fee,
        taker_fee=taker_fee if taker_fee is not None else instrument.taker_fee,
    )


def _precision_to_increments(price_precision: int, size_precision: int) -> tuple[str, str]:
    """Convert precision integers to increment strings.

    precision 0 → "1", precision 1 → "0.1", precision 2 → "0.01", etc.
    """
    price_increment = "1" if price_precision == 0 else "0." + "0" * (price_precision - 1) + "1"
    size_increment = "1" if size_precision == 0 else "0." + "0" * (size_precision - 1) + "1"
    return price_increment, size_increment
