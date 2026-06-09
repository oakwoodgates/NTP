"""A/B test scaffold: project-side LiquidationAware mixin vs NT 1.228's
native ``BacktestVenueConfig.liquidation_enabled`` engine.

**Status.** SKIPPED on NT < 1.228 — the native engine doesn't exist
upstream until 1.228 (see ``docs/SANDBOX_PARTIAL_FILL_AUDIT.md`` notes
on the 1.228 review). This file is committed as part of the
**preparation PR** for the 1.228 upgrade so the upgrade PR has a
ready-to-flip test it can enable in one diff line.

**What this will measure once enabled.**

Two backtests over the same synthetic bar series and same strategy:

* **Run A** — our :class:`LiquidationAware` mixin enabled, NT native
  ``liquidation_enabled=False``. The current 1.227 status quo.
* **Run B** — mixin disabled, NT native ``liquidation_enabled=True``.
  The 1.228 replacement candidate.

We assert that the two runs produce equivalent liquidation behaviour:

* Both close the position via liquidation (not via strategy exit, not
  by reaching the bar series end).
* Liquidation timestamps fire within 1 bar of each other.
* Realised PnL is within 0.5% across the two runs.

If those parity checks pass on 1.228, the upgrade PR can drop
``LiquidationAware`` from backtests (paper/live keep it — Hyperliquid
handles its own liquidation but our mixin is still the canonical
trigger for ``AccountAliveMonitor`` halting the node).

**Scenario design** (kept tight to isolate the liquidation mechanism):

* Single instrument (BTC perp), single short bar series.
* Strategy opens ONE long position at bar 2, then holds — no protective
  stop, no take profit, no other exit logic.
* Bar prices: flat at ~$100k, then a sharp downward leg large enough
  to breach maintenance margin at 20x leverage (~50% drop required for
  ``mm_rate=0.005`` on cross-margin).
* Sizing: ``starting_capital=1000``, ``trade_notional=2000``,
  ``leverage=20``. Same knobs the live trader uses.

If the scenario design needs tuning when this test goes live (1.228),
update both runs together and re-document here why.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.constants import HYPERLIQUID_VENUE

_NT_128_REASON = (
    "BacktestVenueConfig.liquidation_enabled lives in NT >= 1.228. "
    "This file is the preparation-PR scaffold; the upgrade PR removes "
    "this skipif marker and unskips both tests below. See "
    "docs/LIQUIDATION_AND_SIZING.md (post-upgrade revision) for the "
    "A/B comparison's pass criteria."
)


def _nt_at_least_128() -> bool:
    """Return True iff installed NT supports native backtest liquidation.

    Detected by presence of ``liquidation_enabled`` on
    ``BacktestVenueConfig``. Lazy import so this works on 1.227.
    """
    try:
        from nautilus_trader.backtest.config import BacktestVenueConfig
    except Exception:
        return False
    # The field arrived together with native liquidation in 1.228.
    return "liquidation_enabled" in BacktestVenueConfig.__struct_fields__


pytestmark = pytest.mark.skipif(
    not _nt_at_least_128(),
    reason=_NT_128_REASON,
)


def test_baseline_mixin_liquidates_the_position() -> None:
    """Run A only — establishes a baseline that's also useful in 1.227.

    Sanity-checks the mixin's behaviour on the scenario in isolation, so
    when the 1.228 parity test below runs we know the baseline arm is
    well-formed. Skipped on 1.227 along with the parity test because
    the prep PR keeps the file's two tests as a unit (otherwise pytest
    collects one, skips the other, and the file looks half-broken in CI).
    """
    pytest.skip(
        "Implement together with test_native_liquidation_matches_mixin "
        "during the NT 1.228 upgrade PR. The scenario design is in the "
        "module docstring; this test is the Run-A arm.",
    )


def test_native_liquidation_matches_mixin() -> None:
    """Run B vs Run A — the parity check that decides if we drop the mixin.

    Acceptance:

    * Both runs close the position via liquidation
      (``Position.close_reason`` indicates liquidation in both — exact
      string TBD after we see what NT's native engine emits in 1.228).
    * Liquidation ``ts_closed`` within 1 bar across the two runs.
    * ``realized_pnl`` within ``Decimal("0.005")`` (0.5%) across the
      two runs.

    Failure modes worth checking when this fails:

    * Mismatch in maintenance-margin formula — NT's native engine
      may compute the trigger ratio differently from our
      ``LeveragedMarginModel``; see ``crates/risk/src/engine/mod.rs:1314-1401``
      in the 1.228 source tree for the upstream formula.
    * Different fill price on the liquidation order — our mixin uses
      :class:`StopMarketOrder` at the computed liquidation price; NT's
      native engine fills at the next-bar OPEN like any other market
      order. On a low-volatility bar these collapse; on a gap bar they
      may diverge meaningfully.
    """
    # Both arms share the same scenario. Use the module-level
    # constants and the project's standard sizing knobs to mirror the
    # live trader's risk surface.
    starting_capital = 1000
    trade_notional = Decimal("2000")
    leverage = 20

    # Touch the imports so a future "lint says unused" autoclean doesn't
    # delete them when the test body is written.
    assert HYPERLIQUID_VENUE is not None
    assert starting_capital > 0
    assert trade_notional > Decimal("0")
    assert leverage > 0

    pytest.skip(
        "Implement during the NT 1.228 upgrade PR — see module docstring "
        "for the A/B comparison's full specification. Build two engines "
        "via src.backtesting.make_engine, run identical Strategy+bars on "
        "each, assert (a) both liquidate, (b) ts_closed within 1 bar, "
        "(c) realized_pnl within 0.5%.",
    )
