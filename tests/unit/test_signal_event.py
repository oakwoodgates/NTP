"""Tests for ``src.core.signal_event.SignalEvent``."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.signal_event import (
    TOPIC_SIGNAL_MA_CROSS,
    TOPIC_SIGNAL_PREFIX,
    SignalEvent,
)


class TestSignalEvent:
    def test_construct_and_read_fields(self) -> None:
        ev = SignalEvent(
            ts_event=1_700_000_000_000_000_000,
            strategy_id="MACross-EMA-10-40",
            instrument_id="BTC-USD-PERP.HYPERLIQUID",
            signal=1,
            fast_value=Decimal("50123.456"),
            slow_value=Decimal("50100.000"),
            acted=True,
            bootstrap=False,
        )
        assert ev.ts_event == 1_700_000_000_000_000_000
        assert ev.strategy_id == "MACross-EMA-10-40"
        assert ev.instrument_id == "BTC-USD-PERP.HYPERLIQUID"
        assert ev.signal == 1
        assert ev.fast_value == Decimal("50123.456")
        assert ev.slow_value == Decimal("50100.000")
        assert ev.acted is True
        assert ev.bootstrap is False

    def test_frozen(self) -> None:
        ev = SignalEvent(
            ts_event=0, strategy_id="s", instrument_id="i",
            signal=-1, fast_value=Decimal(0), slow_value=Decimal(0),
            acted=False, bootstrap=False,
        )
        with pytest.raises((AttributeError, Exception)):
            ev.signal = 1  # type: ignore[misc]

    def test_topic_prefix_matches_concrete_topic(self) -> None:
        """The PersistenceActor subscribes to ``signals.*`` — that prefix
        must match every concrete topic strategies publish to."""
        assert TOPIC_SIGNAL_MA_CROSS.startswith(TOPIC_SIGNAL_PREFIX)
