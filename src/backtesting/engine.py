"""Backtest engine helpers — shared infrastructure for notebook workflows.

Wraps NT's BacktestEngine setup and parameter sweep boilerplate so that
notebooks only need to provide strategy-specific configuration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.objects import Money

if TYPE_CHECKING:
    from collections.abc import Callable

    from nautilus_trader.model.data import Bar
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.instruments import Instrument


def make_engine(
    venue: Venue,
    instrument: Instrument,
    bars: list[Bar],
    starting_capital: int | float,
    log_level: str = "ERROR",
) -> BacktestEngine:
    """Create a configured BacktestEngine with venue, instrument, and data.

    Parameters
    ----------
    venue
        The venue identifier.
    instrument
        The instrument to add.
    bars
        Bar data to feed.
    starting_capital
        Starting balance in USDC.
    log_level
        NT log level. Default ``"ERROR"`` to avoid stdout flooding.

    """
    engine = BacktestEngine(config=BacktestEngineConfig(
        logging=LoggingConfig(log_level=log_level),
    ))
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=None,
        starting_balances=[Money(starting_capital, USDC)],
    )
    engine.add_instrument(instrument)
    engine.add_data(bars)
    return engine


def run_single_backtest(
    venue: Venue,
    instrument: Instrument,
    bars: list[Bar],
    starting_capital: int | float,
    params: dict[str, Any],
    add_strategy: Callable[[BacktestEngine], None],
    log_level: str = "ERROR",
) -> dict[str, Any]:
    """Run one backtest and return a flat dict of results.

    Creates a fresh engine, runs the backtest, extracts stats, detects
    liquidation, and disposes the engine. Designed for parameter sweeps.

    Parameters
    ----------
    venue
        The venue identifier.
    instrument
        The instrument to add.
    bars
        Bar data to feed.
    starting_capital
        Starting balance in USDC.
    params
        Sweep parameters (e.g. ``{"fast": 10, "slow": 50}``).
        Passed through to the returned dict as-is.
    add_strategy
        Callback that receives the engine and must call
        ``engine.add_strategy(...)`` with the desired strategy.
    log_level
        NT log level.

    Returns
    -------
    dict[str, Any]
        Contains all keys from *params* plus ``total_pnl``,
        ``total_pnl_pct``, ``num_positions``, ``final_balance``,
        ``min_balance``, ``error``, and analyzer performance stats.

    """
    eng = make_engine(venue, instrument, bars, starting_capital, log_level)
    add_strategy(eng)

    row: dict[str, Any] = {**params}

    try:
        eng.run()

        a = eng.portfolio.analyzer
        acct = eng.cache.account_for_venue(venue)
        pos = eng.cache.position_snapshots() + eng.cache.positions()

        if acct is None:
            row["error"] = "no account"
            row.update(
                total_pnl=np.nan, total_pnl_pct=np.nan,
                num_positions=len(pos), final_balance=np.nan,
                min_balance=np.nan,
            )
        else:
            a.calculate_statistics(acct, pos)
            balance = float(acct.balance_total(USDC))
            row.update(
                total_pnl=float(a.total_pnl(USDC)),
                total_pnl_pct=float(a.total_pnl_percentage(USDC)),
                num_positions=len(pos),
                final_balance=balance,
                error="",
            )

            # Detect if equity ever hit zero during the run
            acct_report = eng.trader.generate_account_report(venue)
            if not acct_report.empty:
                min_bal = acct_report["total"].astype(float).min()
                row["min_balance"] = min_bal
                if min_bal <= 0:
                    row["error"] = "liquidated"
            else:
                row["min_balance"] = balance

            for stats_name, stats_fn in [
                ("general", a.get_performance_stats_general),
                ("PnL", lambda: a.get_performance_stats_pnls(USDC)),
                ("returns", a.get_performance_stats_returns),
            ]:
                try:
                    for k, v in stats_fn().items():
                        row[k] = v
                except Exception as e:
                    print(f"  Warning: {stats_name} stats failed for {params}: {e}")

    except Exception as e:
        row["error"] = str(e)
        row.update(
            total_pnl=np.nan, total_pnl_pct=np.nan,
            num_positions=0, final_balance=np.nan, min_balance=np.nan,
        )
    finally:
        eng.dispose()

    return row
