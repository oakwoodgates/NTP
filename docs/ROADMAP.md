# Roadmap

The state of the project as Phase 3a closes, and what comes next. Phase
boundaries are gates — each one has a concrete pass/fail criterion before
the next phase begins.

## Where we are now

**Phase 1 — Backtesting infrastructure.** Complete. NT's `BacktestEngine`
wrapped with project conveniences (`make_engine`, `run_sweep`, fee model
overrides, MA-cross + BB + Donchian + MACD-RSI strategies, v2 metrics
schema with realized-PnL-only stats).

**Phase 2 — Paper trading code.** Code complete. Custom `PersistenceActor`
+ `AlertActor` write fills/positions to PostgreSQL and send Telegram
alerts. `run_sandbox.py` runs paper-trading via NT's `SandboxExecutionClient`
against live Hyperliquid market data. Containerized; runs on Digital
Ocean. The original (polling-mode) MACross was validated end-to-end on
this stack; the cross-gated stack is revalidated in Phase 2.5.

**Phase 2.5 wiring — prep complete, deploy pending.** All
infrastructure for paper-trading revalidation is in place:
- `signal_events` PG table + per-bar `SignalEvent` publication from
  MACross (the key Phase 2.5 telemetry — answers the open question
  about cross-gate firing alignment).
- `AccountAliveMonitor` wired into `run_sandbox.py` for the
  halt-on-equity-floor verification.
- `STOP_PCT` and `BOOTSTRAP_ON_DEPLOY` env vars now wired from
  `Settings` into `MACrossConfig` (previously code-only knobs that
  silently no-op'd at runtime regardless of `.env`).
- Multi-instrument compose topology: profile-gated `trader-eth` /
  `trader-btc` / `trader-sol` services, each reading per-instrument
  strategy config from gitignored `.env.{asset}` files.
- Verification config picked locally (see project-local
  `reports/decisions/`); the picks are deployment-host-only and stay
  off GitHub per the findings-stay-local rule.

What's left for Phase 2.5: actually deploy and run for 1-2 weeks.

**Phase 3a — Research tooling.** Complete (this milestone). Closed with:

- `run_sweep` parameter-grid runner with auto-Parquet persistence
- `walk_forward` analysis with stitched out-of-sample equity
- Bootstrap PnL / max-drawdown confidence intervals
- Regime detection (ADX-based) and per-regime stats
- Fee sensitivity analysis with breakeven detection
- Cross-instrument / cross-timeframe comparison via `compare_sweeps.ipynb`
- 8-check go/no-go validate flow per (instrument, combo) via `validate_strategy.ipynb`
- `validate_all.ipynb` strategy-level matrix consolidator
- v2 tearsheet template (no broken returns-based stats)
- TradingView Lightweight Charts HTML reports with marker accuracy
  fixes (cross-gate-aware, side-aware stop visuals, per-fill OID
  attribution)
- `LiquidationAware` + `ProtectiveStopAware` strategy mixins
- `AccountAliveMonitor` actor halting on equity breach
- Cross-gate entry semantics in `MACross` (signal-event-driven, not
  state-polled)
- `scripts/batch_backtest.py` headless cross-product runner with
  embedded sweep heatmaps and master-index summary

**Honest assessment.** Backtests are now internally consistent and the
metrics they report are trustworthy *given the bar-only-backtest fill
model*. The remaining unknowns are about the model itself, which we can
only resolve by running paper trades and comparing — that's Phase 2.5
and 2.6.

---

## Phase 2.5 — Paper-trading revalidation

**Goal:** confirm the cross-gated `MACross` + protective-stop +
liquidation-simulator stack behaves the same in paper as in backtest,
end to end.

**Two-stage flow:** Stage A on sandbox (simulated fills against real
HL data feed, ~1 week of smoke-test), Stage B on HL testnet (real
testnet exchange, real fills/rejections/latency, ~2 weeks of
revalidation). See [`PAPER_TRADING_GUIDE.md`](PAPER_TRADING_GUIDE.md)
section 5b for operational detail; [`DEPLOY.md`](DEPLOY.md) for the
DO-specific commands.

**Topology:** multi-instrument compose (profile-gated `trader-eth` /
`trader-btc` / `trader-sol` containers, one per instrument). The
verification config — picked from data and recorded in a
project-local `reports/decisions/SANDBOX_CONFIG_VERIFICATION.md` (off
GitHub per the findings-stay-local rule) — is deliberately distinct
from the live-target config. The verification config maximizes event
volume in a 2-week window (more trades = sharper Phase 2.6 statistics);
the live-target config (also picked locally) optimizes cross-instrument
robustness for actual capital deployment.

**Concrete steps:**

1. On the DO box, populate `.env.eth` / `.env.btc` / `.env.sol` from
   committed templates (`.env.{asset}.example`) using the picks from
   the local decision doc. Base `.env` carries shared infrastructure
   + sizing knobs (`STARTING_CAPITAL`, `TRADE_NOTIONAL`, `LEVERAGE`,
   `STOP_PCT`, `BOOTSTRAP_ON_DEPLOY=false`).
2. Stage A: `docker compose --profile eth up -d trader-eth` (and
   similarly for btc/sol, or `--profile multi` for all three). Run
   ~1 week in sandbox mode (`TRADING_SCRIPT=scripts/run_sandbox.py`).
3. Verify wiring within first bar window: `signal_events` rows
   accumulating, `AccountAliveMonitor started` log line present, per-bar
   `cross_gate:` log line firing, zero `blocked-callback` warnings.
4. Verify Telegram alerts fire on fills + position changes + drawdown
   threshold breaches.
5. Stage B: flip `.env` to live mode (`TRADING_SCRIPT=scripts/run_live.py`,
   `HL_TESTNET=true`, `HL_PRIVATE_KEY` set, `LIVE_CONFIRM=yes`),
   `docker compose restart`. Run ~2 weeks on HL testnet.

**Pass criteria for Phase 2.6:**

- Sufficient closed positions to spot-check the persistence + alert
  pipeline (originally targeted ≥20; multi-instrument deploys may
  reach this combined across containers — document the actual count
  + your justification in the verdict doc).
- Every fill in `order_fills` matches an HL testnet UI fill (spot-check 5).
- Zero `Actor blocked-callback` warnings in logs.
- Drawdown alerts confirmed working OR documented reason none fired.
- No unexpected exceptions in the trader process.
- At least one protective-stop fill present (verifies the post-PR-#29
  `STOP_PCT` wiring fires in live conditions).

**Open question to resolve here:** does the cross-gate signal-detection
fire in live in the same place it fires in backtest, given the lag
between bar-close on NT vs bar-close on the venue's actual feed? The
`signal_events` stream + `notebooks/paper_vs_backtest.ipynb` answer
this directly.

---

## Phase 2.6 — Backtest accuracy validation

**The keystone question of this phase: are our backtests accurate?**

This is what enables every downstream decision — go/no-go for live,
parameter selection, strategy promotion, drawdown thresholds. Without
this we're flying blind even with paper data; with it we have a
measurable haircut between backtest predictions and reality.

**Concrete deliverables:**

### Tool 1 — Live-vs-backtest comparison harness *(scaffolded)*

The research notebook `notebooks/paper_vs_backtest.ipynb` exists with
the full join + reporting logic. Helpers live in `notebooks/utils.py`:

- `load_paper_positions(dsn, run_id)` — async PG loader for the
  `positions` table.
- `run_backtest_position_stream(...)` — re-runs `MACross` in
  `BacktestEngine` over a fixed bar window and returns closed positions
  in the same schema.
- `compare_position_streams(paper, backtest, tol_bars=2)` —
  nearest-time-match join, with `entry_time_gap_s`, `entry_px_gap_bps`,
  `pnl_gap` columns + `paper_only` / `bt_only` counts on `.attrs`.

Notebook also supports a `USE_SYNTHETIC_DATA=True` mode (with a
1-bar-lag + 20bps slippage injection on the synthetic paper stream) so
it can be Run-All-d against synthetic data today, before Stage B
real data exists. Unit tests in `tests/unit/test_notebook_utils.py`
pin the contract.

What's missing: real data. Once Stage B accumulates paired positions,
flip the toggle, point at the run_id, and the notebook produces the
fill-time / fill-price / PnL gap distributions.

### Tool 2 — Rolling accuracy metric

Per (strategy, instrument, interval), a continuously-computed
quotient: `actual_pnl / predicted_pnl` and
`actual_drawdown / predicted_drawdown` over rolling windows.

When that ratio drifts past a threshold (initial guess: 30%), flag as
a divergence event and write to a regression-tracker table. Lets us
see "this strategy was fine in paper for 3 weeks, then accuracy
collapsed last week — what changed?"

### Tool 3 — Regression suite for backtest fidelity

`scripts/check_accuracy.py --run-id <X>`. Run a strategy in backtest
through some date, then continue forward as paper, then compare
backtest's prediction for the paper window vs what paper actually did.
Assert agreement within tolerance. Fails CI if a code change makes
backtest predictions worse.

This is what becomes the gate between "backtest looked good" and
"deploy to live."

**What we expect to learn:**

- True backtest-to-live haircut as a measured number, not the
  documented 30-40% guess
- Which strategy classes survive contact with reality (cross-based)
  vs which don't (anything depending on tight fills)
- How often gappy bars / news bars / wicks cause adverse fills
  materially worse than the backtest predicted (the
  bar-only-backtest gotcha quantified, not just hand-waved)
- Whether `LiquidationAware` mixin's predicted liquidations match
  what HL would actually have liquidated at

**Tools we may need to build:**

- A persistence schema for "paper run metadata" — which strategy,
  config, settings the run is using; we have `strategy_runs` per
  CLAUDE.md but verify it captures everything
- Maybe Grafana panels showing live equity vs backtest-equity
  divergence over time
- An NT version of the backtest engine that *replays* paper-trade
  fills instead of generating its own, so we can isolate "what would
  the strategy have done with the same fills" from "what fills did
  the strategy get"

**Pass criteria for Phase 3:** measured haircut documented per
strategy/instrument; strategies whose live performance is within an
acceptable threshold (call it `(actual_pnl) / (predicted_pnl) > 0.7`)
are eligible for live deployment. Strategies that diverge more go
back to research.

---

## Phase 3 — Live trading (small capital)

Once a strategy passes Phase 2.6, deploy with **minimal real capital**
— $100 to $500 to start, NOT the backtest defaults of $1k.

**Concrete steps:**

1. `.env.live` overrides: `HL_TESTNET=false`, real `HL_PRIVATE_KEY`,
   smaller `STARTING_CAPITAL`, `LIVE_CONFIRM=yes`
2. `AlertActor` sends Telegram on every fill and at drawdown
   thresholds (already exists; verify configured)
3. Auto-shutdown via `AccountAliveMonitor` if equity floor breached
   (already exists; verify configured)
4. **Run alongside paper-trading** for cross-validation — the same
   strategy/config running in both, divergence tracked by Phase 2.6's
   accuracy framework continuously
5. Daily review of Grafana + Telegram log; weekly review of the
   accuracy regression metric

**Pass criteria for Phase 4:** at least one strategy running clean
live for 30 days. "Clean" means: no manual interventions, no
unexpected exceptions, accuracy ratio holds within threshold.

---

## Phase 4 — Multi-strategy + portfolio (later)

Once one strategy has clean live track record, scale out to a
portfolio:

- Spin up a second strategy on a different instrument
- Strategy return correlation matrix — answers "are these diversifying
  or just two views of the same trade?"
- Portfolio-level drawdown management — kill switches that consider
  joint exposure, not per-strategy
- Capital allocation across active strategies (manual at first;
  automation only if it's worth it)

This is also when **`research_tools_correlation.ipynb`** (mentioned as
deferred in `CLAUDE.md`'s Phase 3a future tools) becomes worth
building.

---

## Phase 5 — NT v2 migration (when upstream lands)

Track [NautilusTrader issue
#4042](https://github.com/nautechsystems/nautilus_trader/issues/4042)
for the v2 RFC and roadmap. Things we care about most:

- **Returns-based stats methodology fix.** Currently NT's
  `_calculate_portfolio_returns` zero-pads via daily forward-fill, which
  biases Sharpe / Sortino / Volatility for sparse-trade strategies. See
  [`docs/ANALYZER_RETURNS_CAVEAT.md`](ANALYZER_RETURNS_CAVEAT.md). The
  v2 fix will let us re-add returns-based stats to the v2 tearsheet
  (sweep schema bumps to v3 at that point).
- **Native liquidation simulation.** Currently we DIY this via the
  `LiquidationAware` mixin + `AccountAliveMonitor`. NT v2 may bring
  this in-engine, letting us retire the custom code.
- **Bar-backtest fill model improvements.** Phase 2.6 will quantify how
  much our predictions lose to the optimistic trigger-price fills NT
  does today; v2 may tighten this and shrink the haircut.
- **Breaking changes.** Currently pinned at 1.226.0. Do not upgrade
  without a tested branch — the 1.225 → 1.226 bump itself only required
  migrating Hyperliquid configs from `testnet=` to
  `environment=HyperliquidEnvironment.{TESTNET,MAINNET}`, but bigger
  bumps will need full sweep + verdict re-runs.

Migration is opportunistic, not scheduled.

---

## Tooling we'll likely need to build

| Tool | When | Used by |
|---|---|---|
| Live-vs-backtest comparison harness | Phase 2.6 | `paper_vs_backtest.ipynb` |
| Rolling accuracy regression | Phase 2.6 | `scripts/check_accuracy.py` |
| Paper-run metadata schema | Phase 2.6 | PostgreSQL — extend existing `strategy_runs` |
| Grafana panels for live-vs-backtest divergence | Phase 2.6 | dashboard |
| Strategy correlation matrix | Phase 4 | research notebook |
| Portfolio-level kill switch | Phase 4 | new actor |

We already have:

- Grafana dashboards (Phase 2)
- Telegram alerts via `AlertActor`
- PostgreSQL audit trail
- `strategy_runs` and `order_fills` tables
- `validate_strategy.ipynb` 8-check go/no-go matrix
- `compare_sweeps.ipynb` cross-instrument view
- `scripts/batch_backtest.py` for headless grid runs

---

## Out of scope (intentionally)

These are NOT in the roadmap. Listed so the boundary is explicit:

- **Phase 3b — FastAPI + React frontend.** Still deferred. Grafana +
  Telegram cover the monitoring use cases. A web UI is cosmetic until
  there's a genuine remote-control need (multiple traders, mobile
  oversight, customer dashboards). When/if it's built, it's a clean
  layer on top — `StreamingActor` bridges NT events to Redis Streams,
  FastAPI exposes them. Plumbing already in CLAUDE.md.
- **ML / LSTM / RL** (Phases 4-5 in CLAUDE.md). Far future. Not
  scheduled. Useful only after the basic strategies have clean live
  data to learn from.
- **Custom NT exchange adapters.** Hyperliquid + Binance covers the
  current universe. Adding another venue means writing an adapter,
  which is a real engineering project — defer indefinitely.
- **Tick-level backtesting.** Would close the bar-only-backtest gap
  (the source of Phase 2.6's haircut), but at significant complexity
  and runtime cost. Re-evaluate after we measure how big the haircut
  actually is.
- **Verify-notebook modernization.** The two `notebooks/verify/*.ipynb`
  audit utilities use the old `# Cell N` convention with unstable hash
  IDs. They run rarely; not worth the patch effort until we touch them
  for some other reason.

---

## Decision log

A short list of choices made during Phase 3a that constrain the next
phases. If any of these change, revisit the relevant phase plan.

- **Hyperliquid as exec target.** Live trading targets HL perps. Binance
  is data-source only.
- **`MACross` is the reference strategy.** First strategy through the
  full backtest → paper → live pipeline. Other strategies follow the
  same pattern but ship after MA cross is proven.
- **`STOP_PCT=0.05` at `LEVERAGE=20` is the canonical safety setting**
  (isolated-margin equivalence — worst-case loss per trade equals
  initial margin committed). Override per-deployment via `.env`.
- **`run_sandbox.py` and `run_live.py` read from `Settings`** — same
  values flow from research to paper to live.
- **No FastAPI / web UI until Phase 3b.** Confirmed deferral.
