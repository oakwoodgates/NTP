"""Regression guards for the NT attribute paths used in runner scripts.

The runners (`scripts/run_sandbox.py`, `scripts/run_live.py`) access
`node.kernel.X` to wire actors + risk-engine callbacks. A common
mistake — made by the Phase 2.5 wiring change and only caught at first
deploy — is to write `node.trader.kernel.X` instead. That fails at
runtime with `AttributeError: 'Trader' object has no attribute 'kernel'`
because `Trader` does NOT expose `.kernel`; the kernel lives directly
on `TradingNode`.

These tests pin the correct path at two levels:

1. Static scan of the runner scripts — guard against re-introducing the
   bad pattern.
2. Attribute-existence checks on NT's actual classes — guard against
   the same bug happening in a future NT version that renames things.

Pure-Python tests, no engine construction needed.
"""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER_SCRIPTS = [
    _REPO_ROOT / "scripts" / "run_sandbox.py",
    _REPO_ROOT / "scripts" / "run_live.py",
]


def _strip_comments(source: str) -> str:
    """Drop Python `#` comments from each line so the static scan only
    flags actual code uses, not explanatory comments that mention the
    bad pattern as a warning.

    Naive split-on-`#` — fine here because we're searching for a
    very specific attribute-path string that wouldn't legitimately
    appear inside a string literal in these scripts. If it ever does,
    upgrade this to `tokenize.generate_tokens` to skip token types
    `COMMENT` + `STRING`.
    """
    return "\n".join(line.split("#", 1)[0] for line in source.splitlines())


class TestRunnerScriptsAvoidBadKernelPath:
    """Static scan: neither runner should reference `node.trader.kernel`
    in executable code.

    The kernel lives on `TradingNode`, not on `Trader`. The Phase 2.5
    plan doc documented the wrong path; the resulting commit slipped
    through CI because the existing wiring tests stop at
    `MACrossConfig` construction without actually constructing a
    `TradingNode`. This test catches the same kind of slip going
    forward.
    """

    def test_run_sandbox_uses_node_kernel_not_node_trader_kernel(self) -> None:
        src = (_REPO_ROOT / "scripts" / "run_sandbox.py").read_text(encoding="utf-8")
        code_only = _strip_comments(src)
        assert "node.trader.kernel" not in code_only, (
            "scripts/run_sandbox.py references `node.trader.kernel` in "
            "executable code, which fails at runtime — Trader has no "
            ".kernel attribute. The correct path is `node.kernel.X`."
        )

    def test_run_live_uses_node_kernel_not_node_trader_kernel(self) -> None:
        src = (_REPO_ROOT / "scripts" / "run_live.py").read_text(encoding="utf-8")
        code_only = _strip_comments(src)
        assert "node.trader.kernel" not in code_only, (
            "scripts/run_live.py references `node.trader.kernel` in "
            "executable code, which fails at runtime — Trader has no "
            ".kernel attribute. The correct path is `node.kernel.X`."
        )


class TestNTApiSurfaceForRunners:
    """NT-API regression guards.

    If a future NT upgrade moves `msgbus` or `risk_engine` off the kernel
    (or adds them to Trader), these assertions break loudly here instead
    of at first deploy.
    """

    def test_trader_has_no_kernel_attribute(self) -> None:
        """`Trader` doesn't expose `.kernel` — `node.trader.kernel` is invalid."""
        from nautilus_trader.trading.trader import Trader

        # Class-level check (covers both regular attrs and properties)
        assert not hasattr(Trader, "kernel"), (
            "NT's Trader class has gained a `.kernel` attribute. The runners "
            "use `node.kernel` (on TradingNode); if Trader now exposes kernel "
            "too, the project conventions docstring + comments need updating "
            "to clarify which one to use."
        )

    def test_nautilus_kernel_exposes_msgbus_and_risk_engine(self) -> None:
        """The two kernel attributes the runners depend on must exist."""
        from nautilus_trader.system.kernel import NautilusKernel

        assert hasattr(NautilusKernel, "msgbus"), (
            "NautilusKernel.msgbus is missing — `node.kernel.msgbus.subscribe()` "
            "in scripts/run_sandbox.py will fail."
        )
        assert hasattr(NautilusKernel, "risk_engine"), (
            "NautilusKernel.risk_engine is missing — the AccountAliveMonitor "
            "halt_callback in scripts/run_sandbox.py will fail."
        )

    def test_runner_scripts_exist(self) -> None:
        """Sanity: the scripts the other tests scan must actually be on disk."""
        for script in _RUNNER_SCRIPTS:
            assert script.is_file(), f"missing runner script: {script}"


class TestRunLiveEnablesStartupReconciliation:
    """Pin `reconciliation=True` for live trading.

    The live runner persists exchange-side state (positions, protective
    stops) on Hyperliquid. On restart (crash, redeploy, migration) the
    in-process cache is empty but the venue still holds the position.
    Without startup reconciliation the strategy treats the account as
    flat and the next cross signal doubles the position; the prior
    reduce-only stop is left orphaned.

    NT's `LiveExecEngineConfig.reconciliation` defaults to True, but the
    runner sets it explicitly so:

    1. The intent is visible in the file.
    2. This test can pin it — a future PR that flips it to False (or
       drops the explicit config and lets a future NT default change
       silently disable it) trips this guard.

    Sandbox (`run_sandbox.py`) intentionally sets `reconciliation=False`
    because the simulated exchange has no server-side state to query.
    Asking the sandbox executor to reconcile would error; do not touch.
    """

    def test_run_live_sets_reconciliation_true_explicitly(self) -> None:
        src = (_REPO_ROOT / "scripts" / "run_live.py").read_text(encoding="utf-8")
        code_only = _strip_comments(src)
        assert "reconciliation=True" in code_only, (
            "scripts/run_live.py must explicitly set "
            "`LiveExecEngineConfig(reconciliation=True, ...)`. Without it, "
            "a restart in live trading leaves the cache empty while the "
            "exchange still holds the open position — the next cross signal "
            "doubles position size and orphans the protective stop."
        )
        assert "reconciliation=False" not in code_only, (
            "scripts/run_live.py must NOT set `reconciliation=False`. That "
            "is the sandbox-only setting (run_sandbox.py); using it on a "
            "real venue causes doubled positions on restart."
        )
        assert "LiveExecEngineConfig" in code_only, (
            "scripts/run_live.py must import and construct LiveExecEngineConfig "
            "explicitly rather than relying on the NT default — pinning it "
            "in source guards against a silent default change in a future NT "
            "version."
        )

    def test_nautilus_live_exec_engine_config_exposes_reconciliation(self) -> None:
        """NT's LiveExecEngineConfig must keep its `reconciliation` field.

        If a future NT upgrade renames or removes this knob, the runner
        line above silently becomes a no-op. Catch it here.
        """
        from nautilus_trader.live.config import LiveExecEngineConfig

        cfg = LiveExecEngineConfig(reconciliation=True)
        assert cfg.reconciliation is True, (
            "LiveExecEngineConfig.reconciliation no longer accepts True or "
            "no longer exists — the live runner needs updating to match the "
            "new NT API."
        )
