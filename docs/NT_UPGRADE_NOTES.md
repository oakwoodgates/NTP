# NautilusTrader Upgrade Notes

A running log of NT version evaluations: what we checked, what we found,
and why we did or didn't upgrade. The point is to avoid re-doing the
same investigation — and to avoid repeating the same mistakes — every
time a new NT version drops.

Read this before evaluating a new NT release.

---

## The trap: the Rust crate tree is NOT the shipped Python wheel

**This is the single most important thing on this page.** NT's repo
(and the source tarball under `.ref/nautilus_trader-<ver>/`) contains
two very different things:

1. **`crates/**/*.rs`** — the Rust core. This is where NT v2 is being
   built. Release notes increasingly describe fixes here.
2. **`nautilus_trader/**/*.pyx` + `*.pyd`** — the Cython "Python v1"
   engine. **This is what `pip install nautilus_trader` actually runs
   today.**

The Rust core IS statically linked into the wheel (it's why
`engine.cp3xx-win_amd64.pyd` is ~75 MB and why `nautilus_pyo3.pyd`
exists), **but the v1 Python API does not route through it for the
hot paths we use** (BacktestEngine, SimulatedExchange, RiskEngine,
PortfolioAnalyzer). A fix landing in `crates/execution/src/...` does
**nothing** for us until NT flips the v1 Cython API to call the Rust
core — which is the actual v1→v2 cutover (tracked at
[issue #4042](https://github.com/nautechsystems/nautilus_trader/issues/4042)).

**Consequence for changelog reviews:** reading the Rust source or the
release notes is NECESSARY but NOT SUFFICIENT. A claimed fix is only
real for us if it is present in one of:

- the shipped `.pyx` source (`grep` the relevant `.pyx`), OR
- the runtime object's methods (`dir()` / introspection), OR
- empirical execution (run the actual scenario and observe behaviour).

If the only evidence is a `crates/**/*.rs` diff, treat the fix as
**not yet available to us**.

### Verification recipe for any NT version

When evaluating NT `<new>`:

```bash
# 1. Install into the venv (do this in a throwaway worktree).
.venv/Scripts/python -m pip install nautilus_trader==<new>

# 2. For each "fix" you care about, confirm it in the PYTHON layer:

#    a) Is the Cython source actually changed?
grep -n "<marker>" .venv/Lib/site-packages/nautilus_trader/<path>.pyx

#    b) Is the fix compiled into the .pyd (vs only in the linked Rust)?
#       Rust local-variable names usually do NOT survive to strings in a
#       release build, so absence is weak evidence; presence of a fix's
#       distinctive *Python-visible* string (a config field, an error
#       constant) is stronger. Scan with a quick Python string-extractor.

#    c) Does the runtime object expose the capability at all?
.venv/Scripts/python -c "from nautilus_trader... import X; print([m for m in dir(X) if 'liquid' in m.lower()])"

#    d) EMPIRICAL — run the actual scenario and observe. This is ground
#       truth and overrides a/b/c.

# 3. Run our regression contract tests against <new>:
.venv/Scripts/python -m pytest tests/integration/test_sandbox_partial_fill.py -v
.venv/Scripts/python -m pytest tests/integration/test_native_liquidation_engine.py -v

# 4. Downgrade the venv back to the pinned version when done.
.venv/Scripts/python -m pip install nautilus_trader==<pinned>
.venv/Scripts/python -m pip install -e . --no-deps
```

The `test_sandbox_partial_fill.py` and `test_native_liquidation_engine.py`
tests are designed to FLIP on the version where the upstream fix becomes
real (the partial-fill repro starts FAILING; the native-liquidation
scaffold stops skipping). That flip is the signal that the upgrade is
finally worth doing.

---

## 1.228.0 — evaluated, SKIPPED (staying on 1.227.0)

**Date:** 2026-06-09. **Pinned version unchanged:** `1.227.0`.

### Summary

1.228.0's Python wheel is **functionally identical to 1.227.0 for every
hot path we touch.** It is an NT-v2-prep release: the Rust core is
bundled and being built out, but the v1 Cython API we run has not been
switched over. **Zero workarounds can be deleted.** The full test suite
passes on both versions (722/722), so the bump is *safe* — just
*valueless* right now.

A first-pass changelog review (reading `crates/**/*.rs` + release notes)
concluded the opposite — that three of our four big workarounds were
fixed upstream. That conclusion fell into the trap above: the fixes are
in the Rust crate tree, not the shipped Python wheel. The findings below
are the corrected, evidence-backed verdict.

### Findings (each verified in the Python layer, not just the Rust source)

| # | Workaround / caveat | Rust crate tree | **Shipped Python wheel** | Action |
|---|---|---|---|---|
| 1 | Sandbox partial-fill race (`src/adapters/patched_sandbox.py`) | fixed in `crates/execution/.../engine.rs` (`filled_in_loop` gate) | **NOT fixed** | keep |
| 2 | RiskEngine margin not enforced (`LiquidationAware` mixin) | fixed in `crates/risk/.../mod.rs` | **NOT fixed** | keep |
| 3 | Native backtest liquidation (`BacktestVenueConfig.liquidation_enabled`) | exists in Rust core | **dead field in v1 API** | n/a |
| 4 | Analyzer returns-stats bias (`docs/ANALYZER_RETURNS_CAVEAT.md`) | `statistic.rs` daily-bin compounding fix | **NOT fixed** (fix is downstream of the bug) | keep |

**Evidence per finding:**

1. **Partial-fill race — NOT fixed.**
   - `backtest/engine.pyx` slip-fill gate (~line 7411) is byte-identical
     to 1.227: bare `order.is_open_c()`, no `filled_in_loop` clause.
   - `filled_in_loop` (the Rust fix's distinctive token) is **absent**
     from the compiled `engine.pyd` string table.
   - `test_default_fillmodel_leaves_order_partially_filled_zombie`
     **PASSES** on 1.228 (the bug reproduces).

2. **RiskEngine margin — NOT fixed.**
   - `risk/engine.pyx` (~line 688) still returns `True` with the comment
     `# TODO: Determine risk controls for margin`. The pre-trade margin
     check is still short-circuited for MARGIN accounts.

3. **Native liquidation — unreachable dead field.**
   - `BacktestEngine.add_venue()` (what `make_engine` calls) has **no**
     `liquidation_enabled` parameter — we couldn't pass it if we wanted.
   - `grep liquidation_enabled` across the Python source: defined only in
     `backtest/config.py`; **consumed nowhere** in any `.pyx`.
   - Runtime introspection: `SimulatedExchange` has **no liquidation
     method** (`liquidity_consumption` is order-book depth, unrelated;
     there is no `liquidate` / `maintenance_margin` / `check_liquidation`).
   - Conclusion: the config field is a forward-declaration for NT v2.

4. **Analyzer returns-stats — NOT fixed (empirically).**
   - `analysis/analyzer.py` `_returns()` (~lines 638-647) still uses the
     exact `.resample("D").last().ffill().pct_change()` pipeline our
     caveat documents. The `.ffill()` zero-pads every non-trading day to
     a 0% return.
   - The `statistic.rs` "daily-bin compounding" fix cited in the release
     notes operates **downstream** of these already-zero-padded daily
     returns — it's a no-op for our code path (the returns are already
     daily-resolution by the time any compounding happens).
   - Empirical reproduction on 1.228 (synthetic 38-trades-over-2300-days
     sparse strategy, the documented MACrossTakeProfit shape):

     | Metric | Value |
     |---|---|
     | Zero-padded days | 2035 / 2072 (98.2%) |
     | Sharpe (analyzer pipeline) | 0.690 |
     | Sharpe (actual trade returns only) | 5.381 |
     | **Bias** | **7.8× understated** |

     That ~8× understatement matches the documented caveat (1.235 → 0.159).

### Decision

**Stay on 1.227.0.** Nothing to gain. The real payoff — deleting
`src/adapters/patched_sandbox.py`, dropping `LiquidationAware` from
backtests, retiring the analyzer caveat — lands when NT flips the v1
Python API to call the Rust core (the v2 cutover). Re-evaluate on the
next release using the recipe above; watch the release notes for
"Python v2 BacktestEngine", "Rust matching engine default", or similar
v1→v2 language. **That** is the upgrade worth doing.

### Prep work that survives (not wasted)

The 1.228 prep PR shipped two things that are correct regardless of when
we upgrade:

- `TradingNodeConfig(timeout_connection=120.0)` pinned in both runners —
  a sane explicit value (NT's default drifts between versions: 120s in
  1.227, 60s in 1.228).
- `tests/integration/test_native_liquidation_engine.py` — skips on
  NT < 1.228 via a `BacktestVenueConfig.__struct_fields__` probe;
  auto-activates the day the native engine becomes real.
