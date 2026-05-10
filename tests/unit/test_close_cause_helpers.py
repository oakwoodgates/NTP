"""Unit tests for close-cause classification helpers in notebooks/utils.py.

Covers:
- ``classify_position_exits``: bucketing closed positions by close cause
  (strategy_exit / protective_stop / liquidation) via order-tag lookup.
- ``find_account_liq_culprit``: identifying which open positions caused
  the account-level liquidation event.

These helpers are notebook-private analysis support — used to feed the
chart + tearsheet annotations downstream.  Pure logic on synthetic
positions/events; no NT engine required at test time.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pandas as pd

if TYPE_CHECKING:
    import pytest

# Add notebooks/ to sys.path so the import resolves.
_NOTEBOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "notebooks"
sys.path.insert(0, str(_NOTEBOOKS_DIR))

from utils import classify_position_exits, find_account_liq_culprit  # type: ignore[import-not-found]  # noqa: E402, I001


# ── Fixtures ───────────────────────────────────────────────────────────────


def _make_position(
    *,
    pos_id: str = "POS-1",
    closed: bool = True,
    side: str = "PositionSide.LONG",
    avg_px_open: float = 100.0,
    avg_px_close: float | None = 95.0,
    realized_pnl: Decimal | None = Decimal("-50"),
    closing_order_id: str | None = "ORDER-CLOSE-1",
    ts_opened: int = 1_000_000_000_000,
    ts_closed: int | None = 2_000_000_000_000,
) -> SimpleNamespace:
    """Build a minimal Position-like object for testing.

    NT's Position has many attributes; the helpers only touch a small
    subset.  ``SimpleNamespace`` is enough.
    """
    pnl_obj = None
    if realized_pnl is not None:
        pnl_obj = SimpleNamespace(as_decimal=lambda r=realized_pnl: r)

    last_event = (
        SimpleNamespace(client_order_id=closing_order_id)
        if closing_order_id
        else None
    )

    return SimpleNamespace(
        id=pos_id,
        is_closed=closed,
        side=side,
        avg_px_open=avg_px_open,
        avg_px_close=avg_px_close,
        realized_pnl=pnl_obj,
        ts_opened=ts_opened,
        ts_closed=ts_closed,
        events=[last_event] if last_event else None,
        last_event=last_event,
    )


def _make_engine_with_orders(
    orders_by_id: dict[str | None, SimpleNamespace | None],
) -> MagicMock:
    """Build a mock engine whose ``cache.order(id)`` returns the given map."""
    engine = MagicMock()
    engine.cache.order.side_effect = lambda oid: orders_by_id.get(oid)
    return engine


def _make_order(
    *,
    tags: list[str] | None = None,
    avg_px: float | None = 95.0,
    trigger_price: float | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        tags=tags,
        avg_px=avg_px,
        trigger_price=trigger_price,
    )


# ── classify_position_exits ────────────────────────────────────────────────


class TestClassifyPositionExits:
    def test_empty_positions_returns_empty_dataframe(self) -> None:
        engine = _make_engine_with_orders({})
        out = classify_position_exits([], engine)
        assert isinstance(out, pd.DataFrame)
        assert len(out) == 0

    def test_open_positions_excluded(self) -> None:
        """Open (non-closed) positions don't appear in the output."""
        positions = [_make_position(pos_id="OPEN-1", closed=False)]
        engine = _make_engine_with_orders({})
        out = classify_position_exits(positions, engine)
        assert len(out) == 0

    def test_untagged_order_classified_as_strategy_exit(self) -> None:
        positions = [_make_position(pos_id="POS-1", closing_order_id="O-1")]
        engine = _make_engine_with_orders({"O-1": _make_order(tags=None)})
        out = classify_position_exits(positions, engine)
        assert len(out) == 1
        assert out.iloc[0]["close_cause"] == "strategy_exit"
        assert out.iloc[0]["position_id"] == "POS-1"
        assert out.iloc[0]["side"] == "LONG"

    def test_empty_tags_classified_as_strategy_exit(self) -> None:
        positions = [_make_position(closing_order_id="O-1")]
        engine = _make_engine_with_orders({"O-1": _make_order(tags=[])})
        out = classify_position_exits(positions, engine)
        assert out.iloc[0]["close_cause"] == "strategy_exit"

    def test_protective_stop_tag_classified(self) -> None:
        positions = [_make_position(closing_order_id="O-1")]
        engine = _make_engine_with_orders({
            "O-1": _make_order(tags=["protective_stop"], trigger_price=95.0),
        })
        out = classify_position_exits(positions, engine)
        assert out.iloc[0]["close_cause"] == "protective_stop"
        assert out.iloc[0]["trigger_px"] == Decimal("95.0")

    def test_liquidation_tag_classified(self) -> None:
        positions = [_make_position(closing_order_id="O-1")]
        engine = _make_engine_with_orders({
            "O-1": _make_order(tags=["liquidation"], trigger_price=50.0),
        })
        out = classify_position_exits(positions, engine)
        assert out.iloc[0]["close_cause"] == "liquidation"
        assert out.iloc[0]["trigger_px"] == Decimal("50.0")

    def test_multiple_tags_protective_stop_wins_first(self) -> None:
        """If both tags present (unusual), protective_stop is checked first."""
        positions = [_make_position(closing_order_id="O-1")]
        engine = _make_engine_with_orders({
            "O-1": _make_order(tags=["protective_stop", "liquidation"]),
        })
        out = classify_position_exits(positions, engine)
        assert out.iloc[0]["close_cause"] == "protective_stop"

    def test_missing_order_falls_back_to_strategy_exit(self) -> None:
        """If cache.order(id) returns None (pruned), defensive fallback."""
        positions = [_make_position(closing_order_id="MISSING-ID")]
        engine = _make_engine_with_orders({"MISSING-ID": None})
        out = classify_position_exits(positions, engine)
        assert out.iloc[0]["close_cause"] == "strategy_exit"
        assert out.iloc[0]["fill_px"] == Decimal("0")

    def test_missing_closing_order_id_handled(self) -> None:
        """Position with no closing-fill order_id → fallback path."""
        positions = [_make_position(closing_order_id=None)]
        engine = _make_engine_with_orders({})
        out = classify_position_exits(positions, engine)
        assert out.iloc[0]["close_cause"] == "strategy_exit"

    def test_side_extracted_from_position_side_enum(self) -> None:
        """``PositionSide.SHORT`` → ``"SHORT"`` (string after the dot)."""
        positions = [
            _make_position(pos_id="P-LONG", side="PositionSide.LONG",
                           closing_order_id="O-1"),
            _make_position(pos_id="P-SHORT", side="PositionSide.SHORT",
                           closing_order_id="O-2"),
        ]
        engine = _make_engine_with_orders({
            "O-1": _make_order(tags=None),
            "O-2": _make_order(tags=None),
        })
        out = classify_position_exits(positions, engine)
        assert set(out["side"].tolist()) == {"LONG", "SHORT"}

    def test_realized_pnl_extracted(self) -> None:
        positions = [_make_position(
            closing_order_id="O-1", realized_pnl=Decimal("-123.45"),
        )]
        engine = _make_engine_with_orders({"O-1": _make_order(tags=None)})
        out = classify_position_exits(positions, engine)
        assert out.iloc[0]["realized_pnl"] == Decimal("-123.45")

    def test_three_position_mix(self) -> None:
        """End-to-end: 3 positions with 3 different close causes."""
        positions = [
            _make_position(pos_id="P-1", closing_order_id="O-1"),
            _make_position(pos_id="P-2", closing_order_id="O-2"),
            _make_position(pos_id="P-3", closing_order_id="O-3"),
        ]
        engine = _make_engine_with_orders({
            "O-1": _make_order(tags=None),                       # strategy
            "O-2": _make_order(tags=["protective_stop"]),        # protective
            "O-3": _make_order(tags=["liquidation"]),            # liq
        })
        out = classify_position_exits(positions, engine)
        causes = dict(zip(out["position_id"], out["close_cause"], strict=True))
        assert causes == {
            "P-1": "strategy_exit",
            "P-2": "protective_stop",
            "P-3": "liquidation",
        }


# ── find_account_liq_culprit ───────────────────────────────────────────────


def _make_account_liq_event(
    *,
    ts_event: int = 1_500_000_000_000,
    equity: Decimal = Decimal("5.0"),
) -> SimpleNamespace:
    return SimpleNamespace(
        ts_event=ts_event,
        equity=SimpleNamespace(as_decimal=lambda e=equity: e),
    )


def _make_account_report(rows: list[tuple[pd.Timestamp, float]]) -> pd.DataFrame:
    """Build an account-report DataFrame with DatetimeIndex."""
    if not rows:
        return pd.DataFrame()
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame({"total": [r[1] for r in rows]}, index=idx)


class TestFindAccountLiqCulprit:
    def test_no_events_returns_empty_dict(self) -> None:
        out = find_account_liq_culprit([], [], pd.DataFrame())
        assert out == {}

    def test_single_event_single_open_position(self) -> None:
        liq_ts = 1_500_000_000_000  # ns
        ev = _make_account_liq_event(ts_event=liq_ts, equity=Decimal("5.0"))
        # Position open at the liq moment
        pos = _make_position(
            pos_id="P-CULPRIT",
            ts_opened=1_000_000_000_000,
            ts_closed=liq_ts,  # closed AT the liq moment
            avg_px_open=100.0,
            avg_px_close=49.0,
            realized_pnl=Decimal("-510"),
        )
        report = _make_account_report([
            (pd.Timestamp(1_400_000_000_000, unit="ns", tz="UTC"), 515.0),
            (pd.Timestamp(liq_ts, unit="ns", tz="UTC"), 5.0),
        ])
        out = find_account_liq_culprit([ev], [pos], report)
        assert out["liq_ts"] == liq_ts
        assert out["equity_at_liq"] == Decimal("5.0")
        assert out["equity_before"] == Decimal("515.0")
        assert out["drain_amount"] == Decimal("510.0")
        assert out["culprit_position_ids"] == ["P-CULPRIT"]
        assert len(out["culprit_positions"]) == 1
        assert out["culprit_positions"][0]["entry_px"] == Decimal("100.0")

    def test_position_closed_well_before_liq_excluded(self) -> None:
        """A position that closed long before the liq event isn't a culprit."""
        liq_ts = 1_500_000_000_000
        ev = _make_account_liq_event(ts_event=liq_ts)
        old_pos = _make_position(
            pos_id="P-OLD",
            ts_opened=900_000_000_000,
            ts_closed=1_000_000_000_000,  # closed 500ms before liq — not culprit
        )
        culprit = _make_position(
            pos_id="P-CURRENT",
            ts_opened=1_400_000_000_000,
            ts_closed=liq_ts,
        )
        report = _make_account_report([
            (pd.Timestamp(1_490_000_000_000, unit="ns", tz="UTC"), 100.0),
            (pd.Timestamp(liq_ts, unit="ns", tz="UTC"), 5.0),
        ])
        out = find_account_liq_culprit([ev], [old_pos, culprit], report)
        assert out["culprit_position_ids"] == ["P-CURRENT"]

    def test_position_opened_after_liq_excluded(self) -> None:
        """A position that opened after the liq timestamp isn't a culprit."""
        liq_ts = 1_500_000_000_000
        ev = _make_account_liq_event(ts_event=liq_ts)
        future_pos = _make_position(
            pos_id="P-FUTURE",
            ts_opened=liq_ts + 1_000_000,  # 1ms after liq — shouldn't happen but guard
            ts_closed=liq_ts + 10_000_000,
        )
        report = _make_account_report([
            (pd.Timestamp(1_490_000_000_000, unit="ns", tz="UTC"), 100.0),
            (pd.Timestamp(liq_ts, unit="ns", tz="UTC"), 5.0),
        ])
        out = find_account_liq_culprit([ev], [future_pos], report)
        assert out["culprit_position_ids"] == []

    def test_open_position_at_liq_included(self) -> None:
        """A position still OPEN (no ts_closed) at liq time is a culprit."""
        liq_ts = 1_500_000_000_000
        ev = _make_account_liq_event(ts_event=liq_ts)
        pos = _make_position(
            pos_id="P-OPEN-AT-LIQ",
            ts_opened=1_400_000_000_000,
            ts_closed=None,  # still open
            avg_px_close=None,
            realized_pnl=None,
        )
        report = _make_account_report([
            (pd.Timestamp(liq_ts, unit="ns", tz="UTC"), 5.0),
        ])
        out = find_account_liq_culprit([ev], [pos], report)
        assert out["culprit_position_ids"] == ["P-OPEN-AT-LIQ"]
        assert out["culprit_positions"][0]["fill_px"] == Decimal("0")
        assert out["culprit_positions"][0]["realized_pnl"] == Decimal("0")

    def test_multiple_simultaneous_culprits(self) -> None:
        """Two positions open at the same liq moment — both reported."""
        liq_ts = 1_500_000_000_000
        ev = _make_account_liq_event(ts_event=liq_ts)
        positions = [
            _make_position(pos_id="P-1", ts_opened=1_400_000_000_000,
                           ts_closed=liq_ts),
            _make_position(pos_id="P-2", ts_opened=1_450_000_000_000,
                           ts_closed=liq_ts),
        ]
        report = _make_account_report([
            (pd.Timestamp(1_490_000_000_000, unit="ns", tz="UTC"), 100.0),
            (pd.Timestamp(liq_ts, unit="ns", tz="UTC"), 5.0),
        ])
        out = find_account_liq_culprit([ev], positions, report)
        assert set(out["culprit_position_ids"]) == {"P-1", "P-2"}

    def test_iso_timestamp_returned(self) -> None:
        liq_ts = 1_500_000_000_000
        ev = _make_account_liq_event(ts_event=liq_ts)
        report = _make_account_report([
            (pd.Timestamp(liq_ts, unit="ns", tz="UTC"), 5.0),
        ])
        out = find_account_liq_culprit([ev], [], report)
        # Just verify the format; exact string varies by system
        assert "T" in out["liq_ts_iso"]
        assert "+00:00" in out["liq_ts_iso"] or "Z" in out["liq_ts_iso"]

    def test_empty_account_report_zero_equity_before(self) -> None:
        liq_ts = 1_500_000_000_000
        ev = _make_account_liq_event(ts_event=liq_ts)
        out = find_account_liq_culprit([ev], [], pd.DataFrame())
        assert out["equity_before"] == Decimal("0")
        assert out["drain_amount"] == -out["equity_at_liq"]

    def test_multiple_events_warns_uses_first(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        ts_1 = 1_500_000_000_000
        ts_2 = 1_600_000_000_000
        events = [
            _make_account_liq_event(ts_event=ts_1, equity=Decimal("5.0")),
            _make_account_liq_event(ts_event=ts_2, equity=Decimal("3.0")),
        ]
        report = _make_account_report([
            (pd.Timestamp(ts_1, unit="ns", tz="UTC"), 5.0),
        ])
        out = find_account_liq_culprit(events, [], report)
        assert out["liq_ts"] == ts_1  # first one used
        captured = capsys.readouterr()
        assert "Multiple AccountLiquidated events" in captured.out
