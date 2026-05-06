"""Liquidation simulation types and formulas.

Pure functions for computing cross-margin liquidation prices and the
account-alive predicate.  No NT engine dependency — these are used by
the ``LiquidationAware`` strategy mixin (``src/core/liquidation_mixin.py``)
and the ``AccountAliveMonitor`` actor (``src/actors/account_alive.py``)
and by unit tests directly.

`LiquidationConfig` is a `NautilusConfig` (msgspec struct) so it can be
embedded as a field on a `StrategyConfig`.

`PositionLiquidated` and `AccountLiquidated` are project-internal
`@dataclass` events published on the MessageBus by topic for the sweep
runner to count.  They are not NT `Data` subclasses; we publish via
``msgbus.publish(topic=..., msg=event)`` which accepts any object.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nautilus_trader.common.config import NautilusConfig
from nautilus_trader.core.rust.model import PositionSide

# Topics used by the mixin and actor to publish on the MessageBus.
# The sweep runner subscribes to these.
TOPIC_POSITION_LIQUIDATED = "liquidation.position"
TOPIC_ACCOUNT_LIQUIDATED = "liquidation.account"


class LiquidationConfig(NautilusConfig, frozen=True):
    """Configuration for the liquidation simulator.

    Embedded as a field on strategy configs (e.g. ``MACrossConfig.liquidation``).
    Fully resolved by ``make_engine`` before being handed to the strategy:
    ``mm_rate`` falls through to ``VenueConfig.mm_rate`` when ``None``,
    ``min_trade_notional`` falls through the project-wide resolution order.

    Attributes
    ----------
    enabled : bool
        Whether liquidation simulation is active.  When False, the
        ``LiquidationAware`` mixin is a no-op.
    mm_rate : Decimal | None
        Override maintenance margin rate (fraction of notional).
        When ``None``, the canonical source is ``VenueConfig.mm_rate``,
        resolved by ``make_engine``.
    fee_rate : Decimal | None
        Round-trip fee rate for the account-alive predicate.
        When ``None``, ``make_engine`` resolves to ``instrument.taker_fee``.
    min_trade_notional : Decimal | None
        Minimum notional for the account-alive predicate.
        When ``None``, ``make_engine`` resolves via the order:
        ``SizingConfig.min_notional`` → ``SizingConfig.fixed_notional``
        → ``instrument.min_notional`` → raise.
    alive_trades_buffer : int
        Require equity for N consecutive min-notional entries.
        ``1`` matches live exactly; ``>=2`` gives recovery room.
    halt_on_account_liquidation : bool
        When the alive-predicate flips false, also call the engine's
        halt callback (``RiskEngine.set_trading_state(HALTED)``) so
        subsequent ``submit_order`` calls are denied.

    """

    enabled: bool = True
    mm_rate: Decimal | None = None
    fee_rate: Decimal | None = None
    min_trade_notional: Decimal | None = None
    alive_trades_buffer: int = 1
    halt_on_account_liquidation: bool = True


@dataclass(frozen=True)
class PositionLiquidated:
    """Emitted when a position is force-closed at the maintenance-margin price.

    Carries both the original trigger price (where the mixin set the stop —
    derived from the cross-margin formula at position-open time) AND the
    actual fill price (which may differ due to bar-decomposition gap risk:
    if the bar's wick crosses the trigger by more than one tick, NT fills
    at the synthetic-tick price, not the trigger).

    The slippage between trigger and fill is the simulator's "gap risk"
    quality signal — useful for comparing simulated liquidations to real
    venue behavior.
    """

    instrument_id: str
    side: PositionSide
    entry_price: Decimal
    trigger_price: Decimal      # where we placed the stop trigger
    fill_price: Decimal          # where it actually filled (may differ)
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

    Formula::

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
        For longs: a value below entry (further below = healthier).
        For shorts: a value above entry (further above = healthier).
        When ``equity < notional × mm_rate`` (already past liquidation),
        a long's liq_price is above entry and a short's is below entry —
        the mixin treats this as "already liquidated" and skips submission.

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

    Predicate::

        floor_im   = min_trade_notional / venue_leverage
        fee_buffer = min_trade_notional × fee_rate × 2 × alive_trades_buffer
        alive      = equity >= (floor_im + fee_buffer)

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
