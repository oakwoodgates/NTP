# NT Analyzer Returns Stats Are Unreliable

## Status

**Do not use Sharpe, Sortino, Volatility, or returns-based Profit Factor for strategy selection or go/no-go decisions.** These numbers are unreliable in both NT v1.224.0 and v1.225.0. PnL-section stats (Total PnL, Win Rate, Expectancy, PnL-based Profit Factor) are correct and consistent across versions.

Waiting for an upstream fix in NautilusTrader before 2.0.

## What Changed in v1.225.0

The `analyzer.get_performance_stats_returns()` methodology changed between versions. Discovered during the v1.224.0 to v1.225.0 upgrade when comparing saved notebook outputs.

### Concrete example: MACrossTP on BTCUSDT-PERP.BINANCE 1d

Trade-level stats are identical across versions:

| Metric | v1.224.0 | v1.225.0 |
|--------|----------|----------|
| Total PnL | 329.656 | 329.656 |
| Positions | 38 | 38 |
| Win Rate | 0.658 | 0.658 |
| Expectancy | 8.675 | 8.675 |

Returns stats differ dramatically:

| Metric | v1.224.0 | v1.225.0 |
|--------|----------|----------|
| Sharpe Ratio (252d) | 1.279 | 0.159 |
| Sortino Ratio (252d) | 1.857 | 0.231 |
| Returns Volatility (252d) | 1.955 | 0.024 |
| Returns Profit Factor | 1.221 | 1.214 |

## Why Both Are Wrong

### v1.224.0: Per-position returns, annualized as daily

Computed `realized_pnl / notional_value` per position (38 returns for 38 positions), then annualized with `sqrt(252)` as if each were a single-day observation.

**Problem:** Positions were open for 1-60+ days. Treating a 30-day position return as a 1-day return inflates annualized volatility and Sharpe. Avg Win was 8.3% (position-level), which is not a daily return.

### v1.225.0: Equity pct_change, zero-padded daily

Computes `(equity_after - equity_before) / equity_before` at event timestamps (38 non-zero values), then embeds them into a full daily calendar (2,337 days total), padding all non-event days with zero.

**Problem:** 2,299 zero-return days massively dilute the mean (by factor 38/2337 = 0.016x) while deflating std less (~0.126x). This asymmetric dilution crushes Sharpe from 1.235 (non-zero only) to 0.159.

Worse: 432 of those "zero" days actually had an open position with unrealized P&L changing. They are not true zero-return days.

### What a correct implementation needs

Daily mark-to-market equity curve including unrealized P&L. Every bar day, the equity would reflect the current account balance plus the unrealized value of open positions. Daily returns computed from that curve would capture intra-position volatility without zero-padding artifacts.

NT's `BacktestEngine` account report only records equity at event timestamps (fills, position changes), not daily snapshots. Without daily marks, no correct Sharpe is possible.

## Which Stats to Trust

### Reliable (PnL-section, from `get_performance_stats_pnls()`)

- Total PnL / PnL %
- Max/Avg/Min Winner and Loser
- Expectancy
- Win Rate
- PnL-based Profit Factor (wins sum / losses sum)

### Unreliable (Returns-section, from `get_performance_stats_returns()`)

- Sharpe Ratio (252 days)
- Sortino Ratio (252 days)
- Returns Volatility (252 days)
- Returns-based Profit Factor
- Average Return / Average Win Return / Average Loss Return
- Risk Return Ratio

## Why Not Fix It Ourselves

A proper fix requires daily mark-to-market equity that NT doesn't expose from the BacktestEngine. Partial fixes (using non-zero returns only, or computing from the sparse account report) would still be wrong in different ways. This is core engine functionality that should be fixed upstream.

## Impact on Workflow

- **Strategy comparison:** Use Total PnL, PnL %, Win Rate, Expectancy, and drawdown (from account report min balance). Do not rank by Sharpe.
- **Walk-forward:** OOS PnL and OOS PnL % are reliable. OOS Sharpe in fold results is not.
- **Validation notebook:** Bootstrap confidence intervals on PnL are fine. Any Sharpe-based thresholds should be ignored.
- **Sweep heatmaps:** Use `total_pnl` or `total_pnl_pct` for coloring, not Sharpe.
