"""
Core module — TIGHT SCOPE.

This module contains ONLY:
- Type aliases and newtypes (wrapping NT types, Decimal, etc.)
- Constants (exchange names, fee tiers, shared enums)
- Interface protocols (typing.Protocol ABCs for cross-module contracts)
- Pure utility functions (timestamp conversion, decimal formatting)

This module NEVER imports from other src/ modules.
If something doesn't fit the above list, it belongs in a more specific module.
"""

from src.core.constants import (
    CANDLE_LIMIT,
    HYPERLIQUID_API_URL,
    HYPERLIQUID_VENUE,
    INTERVAL_TO_BAR_SPEC,
    MAKER_FEE,
    SETTLEMENT_CURRENCY,
    TAKER_FEE,
    TS_INIT_DELTAS,
)
from src.core.instruments import make_hyperliquid_perp
from src.core.utils import bar_type_str

__all__ = [
    "CANDLE_LIMIT",
    "HYPERLIQUID_API_URL",
    "HYPERLIQUID_VENUE",
    "INTERVAL_TO_BAR_SPEC",
    "MAKER_FEE",
    "SETTLEMENT_CURRENCY",
    "TAKER_FEE",
    "TS_INIT_DELTAS",
    "bar_type_str",
    "make_hyperliquid_perp",
]
