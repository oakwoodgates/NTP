"""Regression guard against orphan ``strategy_runs`` rows on startup failure.

Yesterday a debug loop produced 51 strategy_runs rows with
``stopped_at = NULL`` because ``node.build()`` raised an AttributeError
between ``_register_run()`` (which inserts the row) and the existing
try/finally around ``node.run()`` (which closes it). Each crash left an
orphan row in PostgreSQL; the Grafana "Active Runs" panel
(``COUNT(*) WHERE stopped_at IS NULL``) read 52 when only 1 trader was
actually running.

The fix wraps everything between ``_register_run`` and the cleanup
branch in a try/finally so ``_close_run`` always runs, even when wiring
or build raises. These tests pin that contract.

We drive a deterministic failure path through ``main()`` by patching:
    - ``get_settings`` → minimal in-memory Settings (no .env, no DB)
    - ``_register_run`` / ``_close_run`` → async no-ops that record calls
    - ``TradingNode`` → MagicMock whose ``.build()`` raises

If ``main()`` ever skips ``_close_run`` on a build failure again, these
tests fail loudly.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

# scripts/ is not a package on disk; mirror the path-injection pattern
# used by test_run_strategy_wiring.py so `import run_sandbox` works.
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from src.config.settings import Settings  # noqa: E402


def _build_settings(**overrides: Any) -> Settings:
    """Settings with .env disabled — fully deterministic for tests."""
    return Settings(_env_file=None, postgres_password="test", **overrides)  # type: ignore[call-arg]


def _install_patches(
    monkeypatch: pytest.MonkeyPatch,
    runner_module: Any,
    *,
    settings: Settings,
    build_exc: BaseException | None = None,
    run_exc: BaseException | None = None,
) -> tuple[list[Any], list[Any], mock.MagicMock]:
    """Patch the runner module's external boundaries.

    Returns the call-recorder lists for ``_register_run`` / ``_close_run``
    plus the MagicMock standing in for ``TradingNode``. The mock lets
    each test inject a failure at ``build()`` or ``run()`` time.
    """
    register_calls: list[Any] = []
    close_calls: list[Any] = []

    async def fake_register_run(*args: Any, **kwargs: Any) -> None:
        register_calls.append((args, kwargs))

    async def fake_close_run(*args: Any, **kwargs: Any) -> None:
        close_calls.append((args, kwargs))

    monkeypatch.setattr(runner_module, "_register_run", fake_register_run)
    monkeypatch.setattr(runner_module, "_close_run", fake_close_run)
    monkeypatch.setattr(runner_module, "get_settings", lambda: settings)

    fake_node = mock.MagicMock()
    if build_exc is not None:
        fake_node.build.side_effect = build_exc
    if run_exc is not None:
        fake_node.run.side_effect = run_exc

    # The TradingNode reference imported at module level in the runner
    # must point to a constructor that returns our mock.
    monkeypatch.setattr(runner_module, "TradingNode", lambda **_: fake_node)

    return register_calls, close_calls, fake_node


# ─── run_sandbox.py ──────────────────────────────────────────────────────


class TestRunSandboxStartupFailureClosesRun:
    """The original incident: ``node.build()`` raises AttributeError, the
    process crashes, but ``_close_run`` was never reached. With the fix
    in place, the row gets stamped before the exception propagates.
    """

    def test_build_failure_still_calls_close_run(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import run_sandbox  # type: ignore[import-not-found]

        register_calls, close_calls, _ = _install_patches(
            monkeypatch,
            run_sandbox,
            settings=_build_settings(),
            build_exc=AttributeError("simulated node.build() failure"),
        )

        with pytest.raises(AttributeError, match="simulated node.build"):
            run_sandbox.main()

        assert len(register_calls) == 1, (
            "expected exactly one _register_run call; got "
            f"{len(register_calls)}"
        )
        assert len(close_calls) == 1, (
            "expected _close_run to be called even though build() raised; "
            f"got {len(close_calls)} calls — the recurrence guard is missing"
        )
        # Same run_id should appear in both — the row inserted by
        # register_run must be the one the finally branch closes.
        register_run_id = register_calls[0][0][1]
        close_run_id = close_calls[0][0][1]
        assert register_run_id == close_run_id

    def test_run_failure_still_calls_close_run(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sanity: the existing try/finally around ``node.run()`` already
        covered this case, but exercise it in the new structure too.
        """
        import run_sandbox

        _, close_calls, _ = _install_patches(
            monkeypatch,
            run_sandbox,
            settings=_build_settings(),
            run_exc=RuntimeError("simulated node.run() failure"),
        )

        with pytest.raises(RuntimeError, match="simulated node.run"):
            run_sandbox.main()

        assert len(close_calls) == 1

    def test_clean_shutdown_calls_close_run(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Normal happy path — ``node.run()`` returns, ``_close_run`` runs once."""
        import run_sandbox

        register_calls, close_calls, _ = _install_patches(
            monkeypatch,
            run_sandbox,
            settings=_build_settings(),
        )

        run_sandbox.main()

        assert len(register_calls) == 1
        assert len(close_calls) == 1

    def test_keyboard_interrupt_calls_close_run(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SIGTERM / Ctrl+C path — KeyboardInterrupt must not skip cleanup."""
        import run_sandbox

        _, close_calls, _ = _install_patches(
            monkeypatch,
            run_sandbox,
            settings=_build_settings(),
            run_exc=KeyboardInterrupt(),
        )

        # KeyboardInterrupt is swallowed at the inner try/except —
        # main() returns normally with `interrupted = True`.
        run_sandbox.main()

        assert len(close_calls) == 1


# ─── run_live.py ─────────────────────────────────────────────────────────


class TestRunLiveStartupFailureClosesRun:
    """Mirror of the sandbox tests against the live runner.

    Live runner has the same orphan-row vulnerability — same fix, same
    contract. ``hl_testnet=True`` + ``hl_private_key="x"`` avoid the
    interactive mainnet confirmation and the missing-key sys.exit.
    """

    def test_build_failure_still_calls_close_run(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import run_live  # type: ignore[import-not-found]

        settings = _build_settings(hl_private_key="fake-key-for-test")
        register_calls, close_calls, _ = _install_patches(
            monkeypatch,
            run_live,
            settings=settings,
            build_exc=AttributeError("simulated node.build() failure"),
        )

        with pytest.raises(AttributeError, match="simulated node.build"):
            run_live.main()

        assert len(register_calls) == 1
        assert len(close_calls) == 1, (
            "run_live: expected _close_run to run after build() raised; "
            "the recurrence guard is missing"
        )

    def test_clean_shutdown_calls_close_run(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import run_live

        settings = _build_settings(hl_private_key="fake-key-for-test")
        _, close_calls, _ = _install_patches(
            monkeypatch,
            run_live,
            settings=settings,
        )

        run_live.main()

        assert len(close_calls) == 1
