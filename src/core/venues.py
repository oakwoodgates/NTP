"""Execution venue configurations for backtest simulation.

A venue config holds the fee structure, leverage, and settlement currency
used to override instrument metadata when simulating trading on a specific
exchange.  This is separate from the data source -- we may use Binance bar
data to simulate Hyperliquid execution.

Add new venues here as a single entry.  All backtest notebooks pull from
this registry rather than hardcoding fee constants per notebook.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class VenueConfig:
    """Execution venue configuration for backtest simulation.

    Attributes
    ----------
    name : str
        Qualified venue name (e.g., "HYPERLIQUID_PERP", "BINANCE_PERP").
    nt_venue : str
        NautilusTrader venue name (e.g., "HYPERLIQUID", "BINANCE").
        Used for ``Venue()`` objects and instrument ID matching.
    maker_fee : Decimal
        Maker fee as a fraction (e.g., Decimal("0.00010") = 1 bp).
    taker_fee : Decimal
        Taker fee as a fraction.
    leverage : int
        Maximum leverage to apply (margin_init = 1 / leverage).
    settlement_currency : str
        Settlement currency code (e.g., "USDC", "USDT").  Informational --
        the actual settlement currency comes from the underlying instrument.

    """

    name: str
    nt_venue: str
    maker_fee: Decimal
    taker_fee: Decimal
    leverage: int
    settlement_currency: str


VENUE_CONFIGS: dict[str, VenueConfig] = {
    "HYPERLIQUID_PERP": VenueConfig(
        name="HYPERLIQUID_PERP",
        nt_venue="HYPERLIQUID",
        maker_fee=Decimal("0.00010"),    # 1 bp   -- HL VIP 0 base tier
        taker_fee=Decimal("0.00035"),    # 3.5 bp
        leverage=20,
        settlement_currency="USDC",
    ),
    "BINANCE_PERP": VenueConfig(
        name="BINANCE_PERP",
        nt_venue="BINANCE",
        maker_fee=Decimal("0.000200"),   # 2 bp  -- Binance Futures VIP 0
        taker_fee=Decimal("0.000500"),   # 5 bp
        leverage=20,
        settlement_currency="USDT",
    ),
    "BINANCE_SPOT": VenueConfig(
        name="BINANCE_SPOT",
        nt_venue="BINANCE",
        maker_fee=Decimal("0.001000"),   # 10 bp -- Binance Spot VIP 0
        taker_fee=Decimal("0.001000"),
        leverage=1,
        settlement_currency="USDT",
    ),
}


def get_venue_config(venue: str) -> VenueConfig:
    """Look up a venue config by name.

    Parameters
    ----------
    venue : str
        Qualified venue identifier (e.g., "HYPERLIQUID_PERP").

    Returns
    -------
    VenueConfig

    Raises
    ------
    ValueError
        If the venue is not registered in VENUE_CONFIGS.

    """
    if venue not in VENUE_CONFIGS:
        known = sorted(VENUE_CONFIGS.keys())
        raise ValueError(f"Unknown venue: {venue!r}. Known: {known}")
    return VENUE_CONFIGS[venue]
