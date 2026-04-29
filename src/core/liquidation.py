"""Liquidation simulation types and formulas.

Pure functions for computing cross-margin liquidation prices and the
account-alive predicate.  No NT engine dependency — these are used by
the LiquidationMonitor Actor (src/actors/liquidation.py) and by unit
tests directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nautilus_trader.core.rust.model import PositionSide


@dataclass(frozen=True)
class LiquidationConfig:
    """Configuration for the LiquidationMonitor Actor.

    Attributes
    ----------
    enabled : bool
        Whether liquidation simulation is active.
    mm_rate : Decimal
        Maintenance margin rate as a fraction of notional (e.g., 0.005 = 0.5%).
    fee_rate : Decimal | None
        Round-trip fee rate for the account-alive predicate.
        None means read from instrument.taker_fee at Actor init.
    min_trade_notional : Decimal
        Minimum notional for the account-alive predicate.
        Account is "dead" when equity can't margin this amount.
    alive_trades_buffer : int
        Require equity for N consecutive min-notional entries.
        1 matches live exactly; >=2 gives recovery room.
    halt_on_account_liquidation : bool
        Whether to halt all trading when account is liquidated.

    """

    enabled: bool = True
    mm_rate: Decimal = Decimal("0.005")
    fee_rate: Decimal | None = None
    min_trade_notional: Decimal = Decimal("10")
    alive_trades_buffer: int = 1
    halt_on_account_liquidation: bool = True


@dataclass(frozen=True)
class PositionLiquidated:
    """Emitted when a position is force-closed at the maintenance-margin price."""

    instrument_id: str
    side: PositionSide
    entry_price: Decimal
    liq_price: Decimal
    realized_pnl: Decimal
    ts_event: int


@dataclass(frozen=True)
class AccountLiquidated:
    """Emitted when equity can no longer fund the next minimum-size entry."""

    equity: Decimal
    required: Decimal
    ts_event: int


def compute_liquidation_price(
    entry_price: Decimal,
    side: PositionSide,
    equity: Decimal,
    notional: Decimal,
    mm_rate: Decimal,
) -> Decimal:
    """Compute the cross-margin liquidation price for a position.

    Formula:
        liq_distance = equity / notional - mm_rate
        long:  entry × (1 - liq_distance)
        short: entry × (1 + liq_distance)

    Parameters
    ----------
    entry_price
        The position's average entry price.
    side
        LONG or SHORT.
    equity
        Account equity at the time of computation.
    notional
        Position notional (abs(quantity) × entry_price).
    mm_rate
        Maintenance margin rate as a fraction of notional.

    Returns
    -------
    Decimal
        The price at which the venue would force-close the position.
        A negative or zero value means the position is already past
        the liquidation threshold.

    """
    liq_distance = equity / notional - mm_rate
    if side == PositionSide.LONG:
        return entry_price * (Decimal("1") - liq_distance)
    return entry_price * (Decimal("1") + liq_distance)


def is_account_alive(
    equity: Decimal,
    min_trade_notional: Decimal,
    venue_leverage: int,
    fee_rate: Decimal,
    alive_trades_buffer: int = 1,
) -> bool:
    """Check whether the account can still margin a minimum-size entry.

    Predicate:
        floor_im   = min_trade_notional / venue_leverage
        fee_buffer  = min_trade_notional × fee_rate × 2 × alive_trades_buffer
        alive       = equity >= (floor_im + fee_buffer)

    Parameters
    ----------
    equity
        Current account equity.
    min_trade_notional
        Minimum notional the strategy would submit.
    venue_leverage
        Account leverage (e.g. 20 for 20x).
    fee_rate
        Taker fee rate (used for round-trip fee estimate).
    alive_trades_buffer
        Require equity for N consecutive min-notional entries.

    Returns
    -------
    bool
        True if the account can still open a position.

    """
    floor_im = min_trade_notional / Decimal(venue_leverage)
    fee_buffer = min_trade_notional * fee_rate * 2 * alive_trades_buffer
    return equity >= (floor_im + fee_buffer)
