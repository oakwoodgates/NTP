"""Regression tests for ``charts._fills_to_markers``.

These guard against three concrete bugs that shipped in the original
close-cause visualisation work:

1. **Marker dedup-by-timestamp** dropped one fill per NETTING-reversal
   collision.  The fix renders every fill, keyed on per-fill order id.
2. **STOP/LIQ visual upgrade gated on ``not is_buy``** missed every
   short-position stop (which closes via a BUY).  The fix drives the
   visual from the close-cause regardless of fill side.
3. **Trade-number / cause lookup keyed by timestamp** silently picked
   the wrong trade's metadata when fills shared a timestamp.  The fix
   keys both lookups on ``client_order_id``.

Tests use synthetic ``fills_df`` rows; no NT engine required.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

# Add notebooks/ to sys.path so the import resolves.
_NOTEBOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "notebooks"
sys.path.insert(0, str(_NOTEBOOKS_DIR))

from charts import _fills_to_markers  # type: ignore[import-not-found]  # noqa: E402, I001


# ── Fixtures ───────────────────────────────────────────────────────────────


def _fill(
    *,
    ts_last_ns: int,
    side: str,
    avg_px: float = 100.0,
    qty: float = 1.0,
    client_order_id: str = "OID-X",
) -> dict[str, object]:
    """One row of a fills_report."""
    return {
        "ts_last":         ts_last_ns,
        "ts_init":         ts_last_ns,
        "side":            side,
        "avg_px":          avg_px,
        "filled_qty":      qty,
        "client_order_id": client_order_id,
    }


def _fills(*rows: dict[str, object]) -> pd.DataFrame:
    return pd.DataFrame(rows)


# ── Bug 1: dedup collapse on simultaneous fills ───────────────────────────


class TestSimultaneousFillsRender:
    """NETTING-reversal collisions must render all fills, not collapse."""

    def test_two_fills_same_ts_distinct_oids_both_render(self) -> None:
        """Close of trade #1 + open of trade #2 share a bar timestamp."""
        ts_ns = 1_700_000_000 * 1_000_000_000
        df = _fills(
            _fill(ts_last_ns=ts_ns, side="BUY",  client_order_id="OID-CLOSE-1"),
            _fill(ts_last_ns=ts_ns, side="SELL", client_order_id="OID-OPEN-2"),
        )
        markers, detail = _fills_to_markers(
            df,
            oid_to_trade_num={"OID-CLOSE-1": 1, "OID-OPEN-2": 2},
            oid_to_close_cause={"OID-CLOSE-1": "protective_stop"},
        )
        # Both fills render — the dedup is gone.
        assert len(markers) == 2
        # Both detail entries survive too — keyed by ts:oid, not ts.
        assert len(detail) == 2
        # And each gets the *right* trade number — no overwrite.
        trade_nums = sorted(d["trade_num"] for d in detail.values())
        assert trade_nums == [1, 2]

    def test_marker_text_distinguishes_simultaneous_fills(self) -> None:
        ts_ns = 1_700_000_000 * 1_000_000_000
        df = _fills(
            _fill(ts_last_ns=ts_ns, side="BUY",  client_order_id="OID-CLOSE-1"),
            _fill(ts_last_ns=ts_ns, side="SELL", client_order_id="OID-OPEN-2"),
        )
        markers, _ = _fills_to_markers(
            df,
            oid_to_trade_num={"OID-CLOSE-1": 1, "OID-OPEN-2": 2},
            oid_to_close_cause={"OID-CLOSE-1": "protective_stop"},
        )
        texts = sorted(m["text"] for m in markers)
        assert texts == ["#1 STOP", "#2"]


# ── Bug 2: STOP/LIQ visual must work for both BUY and SELL fills ─────────


class TestCauseDrivenVisuals:
    """Cause drives marker visual, regardless of fill side.

    A SHORT-position protective stop fires a BUY closing fill.  A
    LONG-position protective stop fires a SELL closing fill.  Both must
    render with the STOP visual (orange circle), not as a regular
    BUY/SELL arrow.
    """

    def test_short_position_stop_buy_fill_uses_stop_visual(self) -> None:
        df = _fills(_fill(
            ts_last_ns=1_700_000_000 * 1_000_000_000,
            side="BUY",
            client_order_id="OID-STOP-SHORT",
        ))
        markers, detail = _fills_to_markers(
            df,
            oid_to_trade_num={"OID-STOP-SHORT": 5},
            oid_to_close_cause={"OID-STOP-SHORT": "protective_stop"},
        )
        assert len(markers) == 1
        m = markers[0]
        assert m["shape"] == "circle"           # STOP visual
        assert m["color"] == "#ff8a65"          # warm orange
        assert "STOP" in m["text"]
        # And the marker should sit ABOVE the bar (BUY at adverse high),
        # not below where regular BUY arrows go.
        assert m["position"] == "aboveBar"

    def test_long_position_stop_sell_fill_uses_stop_visual(self) -> None:
        df = _fills(_fill(
            ts_last_ns=1_700_000_000 * 1_000_000_000,
            side="SELL",
            client_order_id="OID-STOP-LONG",
        ))
        markers, _ = _fills_to_markers(
            df,
            oid_to_trade_num={"OID-STOP-LONG": 5},
            oid_to_close_cause={"OID-STOP-LONG": "protective_stop"},
        )
        m = markers[0]
        assert m["shape"] == "circle"
        assert m["color"] == "#ff8a65"
        # SELL stop fills at adverse low → marker below the bar.
        assert m["position"] == "belowBar"

    def test_short_position_liquidation_buy_fill_uses_liq_visual(self) -> None:
        df = _fills(_fill(
            ts_last_ns=1_700_000_000 * 1_000_000_000,
            side="BUY",
            client_order_id="OID-LIQ-SHORT",
        ))
        markers, _ = _fills_to_markers(
            df,
            oid_to_trade_num={"OID-LIQ-SHORT": 3},
            oid_to_close_cause={"OID-LIQ-SHORT": "liquidation"},
        )
        m = markers[0]
        assert m["shape"] == "square"
        assert m["color"] == "#ff1744"
        assert "LIQ" in m["text"]


# ── Bug 3: trade-number lookup keyed on order id ────────────────────────


class TestPerFillOidLookup:
    """``oid_to_*`` lookups disambiguate simultaneous fills correctly."""

    def test_open_fill_uses_open_trade_num(self) -> None:
        ts_ns = 1_700_000_000 * 1_000_000_000
        df = _fills(_fill(
            ts_last_ns=ts_ns, side="SELL", client_order_id="OPEN-2",
        ))
        markers, _ = _fills_to_markers(
            df,
            oid_to_trade_num={"OPEN-2": 2, "CLOSE-1": 1},
            oid_to_close_cause={"CLOSE-1": "protective_stop"},
        )
        # The fill's OID is OPEN-2 → trade #2 (the new entry), not #1.
        assert markers[0]["text"] == "#2"

    def test_unmatched_oid_falls_back_to_b_or_s_label(self) -> None:
        df = _fills(_fill(
            ts_last_ns=1_700_000_000 * 1_000_000_000,
            side="BUY", qty=0.5, client_order_id="UNKNOWN",
        ))
        markers, _ = _fills_to_markers(
            df,
            oid_to_trade_num={"OTHER": 7},
            oid_to_close_cause={},
        )
        # No trade-number lookup hit, no cause → "B 0.5" label.
        assert markers[0]["text"] == "B 0.5"

    def test_no_lookups_gives_default_visuals(self) -> None:
        """Legacy callers passing no maps still produce sensible markers."""
        df = _fills(
            _fill(ts_last_ns=1_700_000_000 * 1_000_000_000,
                  side="BUY", qty=1.0, client_order_id="X"),
            _fill(ts_last_ns=1_700_000_001 * 1_000_000_000,
                  side="SELL", qty=1.0, client_order_id="Y"),
        )
        markers, _ = _fills_to_markers(df)
        shapes = [m["shape"] for m in markers]
        assert shapes == ["arrowUp", "arrowDown"]


# ── Misc edge cases ────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_fills_returns_empty(self) -> None:
        markers, detail = _fills_to_markers(pd.DataFrame())
        assert markers == []
        assert detail == {}

    def test_none_fills_returns_empty(self) -> None:
        markers, detail = _fills_to_markers(None)
        assert markers == []
        assert detail == {}

    def test_markers_sorted_by_time(self) -> None:
        df = _fills(
            _fill(ts_last_ns=3 * 10**18, side="SELL", client_order_id="A"),
            _fill(ts_last_ns=1 * 10**18, side="BUY",  client_order_id="B"),
            _fill(ts_last_ns=2 * 10**18, side="SELL", client_order_id="C"),
        )
        markers, _ = _fills_to_markers(df)
        times = [m["time"] for m in markers]
        assert times == sorted(times)

    def test_detail_entry_includes_ts_field(self) -> None:
        """Detail rows expose ts so the JS tooltip can group by timestamp."""
        ts_ns = 1_700_000_000 * 1_000_000_000
        df = _fills(_fill(
            ts_last_ns=ts_ns, side="BUY", client_order_id="X",
        ))
        _, detail = _fills_to_markers(df)
        assert len(detail) == 1
        entry = next(iter(detail.values()))
        assert entry["ts"] == ts_ns // 1_000_000_000  # unix seconds

    def test_oid_read_from_index_when_named_client_order_id(self) -> None:
        """NT's generate_order_fills_report() returns a DataFrame indexed by
        client_order_id (no column with that name). _fills_to_markers must
        still pick the OID up so the cause/trade-num lookups land.
        """
        ts_ns = 1_700_000_000 * 1_000_000_000
        df = pd.DataFrame(
            [{
                "ts_last": ts_ns, "ts_init": ts_ns,
                "side": "SELL", "avg_px": 100.0, "filled_qty": 1.0,
            }],
            index=pd.Index(["OID-FROM-INDEX"], name="client_order_id"),
        )
        markers, _ = _fills_to_markers(
            df,
            oid_to_trade_num={"OID-FROM-INDEX": 7},
            oid_to_close_cause={"OID-FROM-INDEX": "protective_stop"},
        )
        assert len(markers) == 1
        assert markers[0]["text"] == "#7 STOP"
        assert markers[0]["shape"] == "circle"
