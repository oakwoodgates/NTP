"""Signal event types for per-bar gate output.

Published on the MessageBus by signal-generating strategies (e.g.
``MACross``) every initialized bar, regardless of whether the gate
actually fired. The ``acted`` flag distinguishes "this is where we
entered/flipped" from "this is the gate state we observed."

Mirrors the ``PositionLiquidated`` / ``AccountLiquidated`` pattern in
:mod:`src.core.liquidation` — plain ``@dataclass(frozen=True)`` published
via ``msgbus.publish(topic=..., msg=event)``.

The PersistenceActor writes one row per event to the ``signal_events``
table, letting Phase 2.5 analysis reconstruct the full signal stream
offline and align it against backtest cross times.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal

# Topic prefix used by signal-generating strategies. The PersistenceActor
# subscribes to ``"signals.*"`` and writes each matching event to PG.
TOPIC_SIGNAL_PREFIX = "signals."
TOPIC_SIGNAL_MA_CROSS = "signals.ma_cross"


@dataclass(frozen=True)
class SignalEvent:
    """One bar's gate output from a signal-generating strategy.

    Attributes
    ----------
    ts_event : int
        Bar event timestamp (ns since epoch, UTC) — the bar this signal
        was computed at the close of. Used to join paper signals against
        backtest signals by bar.
    strategy_id : str
        Strategy instance identifier (``str(strategy.id)``).
    instrument_id : str
        Instrument the signal is about (``str(instrument_id)``).
    signal : int
        Signed gate direction: ``+1`` LONG, ``-1`` SHORT, ``0`` NONE.
    fast_value : Decimal
        Fast-indicator value at the bar's close (e.g. fast MA).
    slow_value : Decimal
        Slow-indicator value at the bar's close (e.g. slow MA).
    acted : bool
        True iff the strategy actually entered or flipped on this bar.
        False rows still get persisted — they're the gate state for
        bars where the cross hadn't transitioned yet.
    bootstrap : bool
        True iff this firing was the bootstrap-on-deploy synthesizer
        rather than a genuine signal transition. Always False after the
        first bootstrap bar.
    bar_close : Decimal
        Closing price of the bar this signal was computed at. Used by
        downstream consumers (dashboard, analysis) to compute
        mark-to-market metrics like unrealized PnL on the open position
        without needing a separate mark-price feed.

    """

    ts_event: int
    strategy_id: str
    instrument_id: str
    signal: int
    fast_value: Decimal
    slow_value: Decimal
    acted: bool
    bootstrap: bool
    bar_close: Decimal
