"""Tests for the ``parent_run_id`` linkage in ``_register_run``.

The runners insert a fresh ``strategy_runs`` row on every process start
and link it to the most-recent prior row for the same
``(trader_id, instrument_id, strategy_id, run_mode)`` tuple via the
nullable ``parent_run_id`` column (Alembic 003).

These tests exercise the SQL contract by stubbing out ``asyncpg.connect``
with a fake connection that records ``fetchval``/``execute`` arguments —
no real database needed.

Covered:

* First-ever run for a tuple → ``parent_run_id IS NULL`` (fetchval returns
  None, INSERT receives None as ``$8``).
* Second run for the same tuple → ``parent_run_id`` equals the prior
  row's id (fetchval returns the prior UUID, INSERT receives it).
* The SELECT filters on all four lookup columns AND sorts DESC, so
  switching ``run_mode`` (sandbox → live) doesn't link rows together.
* Both ``run_sandbox`` and ``run_live`` use the same contract.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

# scripts/ is not a package on disk; mirror the path-injection pattern
# the existing wiring tests use so `import run_sandbox`/`run_live` work.
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


class _FakeConnection:
    """Minimal asyncpg-like connection that records calls.

    ``fetchval`` returns whatever the test seeded as ``parent_id`` (the
    previous run's UUID, or None for first-ever runs). ``execute`` just
    stashes its (query, args) so the test can assert on them.
    """

    def __init__(self, parent_id: uuid.UUID | None) -> None:
        self._parent_id = parent_id
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, query: str, *args: Any) -> uuid.UUID | None:
        self.fetchval_calls.append((query, args))
        return self._parent_id

    async def execute(self, query: str, *args: Any) -> None:
        self.execute_calls.append((query, args))

    async def close(self) -> None:
        pass


def _patch_connect(
    monkeypatch: pytest.MonkeyPatch,
    runner_module: Any,
    *,
    parent_id: uuid.UUID | None,
) -> _FakeConnection:
    """Replace ``asyncpg.connect`` (as used by the runner) with a stub.

    The runner imports ``asyncpg`` at module load; we patch the module's
    bound reference so we don't need to touch the global asyncpg
    package. Returns the (single) fake connection so the test can
    inspect its call log.
    """
    fake = _FakeConnection(parent_id)

    async def _connect(_dsn: str) -> _FakeConnection:
        return fake

    # The runner accesses ``asyncpg.connect`` as an attribute on the
    # imported module, not from a local import — monkeypatching the
    # attribute on the imported asyncpg module is the lightest touch.
    monkeypatch.setattr(runner_module.asyncpg, "connect", _connect)
    return fake


async def _call_register_run(
    runner_module: Any,
    *,
    run_id: uuid.UUID,
    trader_id: str = "nt-trader-eth",
    strategy_id: str = "MACross-EMA-10-100",
    instrument_id: str = "ETH-USD-PERP.HYPERLIQUID",
    mode: str = "sandbox",
) -> None:
    """Invoke ``_register_run`` with sensible defaults for the link tests."""
    await runner_module._register_run(
        "postgresql://fake",
        str(run_id),
        trader_id,
        strategy_id,
        instrument_id,
        mode,
        {"ma_type": "EMA", "fast": 10, "slow": 100},
    )


# ─── run_sandbox.py ──────────────────────────────────────────────────────


class TestRunSandboxParentLink:
    """Sandbox-runner contract for ``_register_run``."""

    @pytest.mark.anyio
    async def test_first_run_has_null_parent(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First run for a tuple: ``SELECT ... LIMIT 1`` returns nothing
        → ``parent_run_id`` is NULL in the INSERT.
        """
        import run_sandbox  # type: ignore[import-not-found]

        fake = _patch_connect(monkeypatch, run_sandbox, parent_id=None)
        new_id = uuid.uuid4()

        await _call_register_run(run_sandbox, run_id=new_id)

        assert len(fake.fetchval_calls) == 1, (
            "expected a single SELECT to look up the parent run"
        )
        assert len(fake.execute_calls) == 1, (
            "expected a single INSERT after the lookup"
        )
        # SELECT filters on the four-tuple key
        select_query, select_args = fake.fetchval_calls[0]
        assert "FROM strategy_runs" in select_query
        assert "trader_id = $1" in select_query
        assert "instrument_id = $2" in select_query
        assert "strategy_id = $3" in select_query
        assert "run_mode = $4" in select_query
        assert "ORDER BY started_at DESC" in select_query
        assert "LIMIT 1" in select_query
        assert select_args == (
            "nt-trader-eth",
            "ETH-USD-PERP.HYPERLIQUID",
            "MACross-EMA-10-100",
            "sandbox",
        )
        # INSERT carries parent_run_id = None
        insert_query, insert_args = fake.execute_calls[0]
        assert "INSERT INTO strategy_runs" in insert_query
        assert "parent_run_id" in insert_query
        # Positional args: (id, trader_id, strategy_id, instrument_id,
        # run_mode, started_at, config, parent_run_id)
        assert insert_args[0] == new_id
        assert insert_args[-1] is None

    @pytest.mark.anyio
    async def test_second_run_links_to_first(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Second run for the same tuple: ``SELECT`` returns the prior
        UUID → ``parent_run_id`` in the INSERT matches.
        """
        import run_sandbox

        prior_id = uuid.uuid4()
        new_id = uuid.uuid4()
        fake = _patch_connect(monkeypatch, run_sandbox, parent_id=prior_id)

        await _call_register_run(run_sandbox, run_id=new_id)

        _, insert_args = fake.execute_calls[0]
        assert insert_args[0] == new_id
        assert insert_args[-1] == prior_id, (
            f"new run {new_id} should link to prior {prior_id}, "
            f"got parent_run_id={insert_args[-1]!r}"
        )

    @pytest.mark.anyio
    async def test_lookup_uses_run_mode_as_part_of_key(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``run_mode`` is in the lookup key so a sandbox run never
        links to a live run (or vice versa) for the same trader. The
        SELECT must pass ``mode`` as ``$4``.
        """
        import run_sandbox

        fake = _patch_connect(monkeypatch, run_sandbox, parent_id=None)

        # Call with mode="live" on the sandbox runner — verifies the
        # lookup signature regardless of which runner invokes it.
        await runner_call_with_mode(
            run_sandbox, mode="live", run_id=uuid.uuid4(),
        )

        _, select_args = fake.fetchval_calls[0]
        assert select_args[3] == "live"


# ─── run_live.py ─────────────────────────────────────────────────────────


class TestRunLiveParentLink:
    """Live-runner contract — identical lookup + insert shape."""

    @pytest.mark.anyio
    async def test_first_run_has_null_parent(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import run_live  # type: ignore[import-not-found]

        fake = _patch_connect(monkeypatch, run_live, parent_id=None)
        new_id = uuid.uuid4()

        await _call_register_run(run_live, run_id=new_id, mode="live")

        assert len(fake.fetchval_calls) == 1
        assert len(fake.execute_calls) == 1
        _, insert_args = fake.execute_calls[0]
        assert insert_args[-1] is None

    @pytest.mark.anyio
    async def test_second_run_links_to_first(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import run_live

        prior_id = uuid.uuid4()
        new_id = uuid.uuid4()
        fake = _patch_connect(monkeypatch, run_live, parent_id=prior_id)

        await _call_register_run(run_live, run_id=new_id, mode="live")

        _, insert_args = fake.execute_calls[0]
        assert insert_args[-1] == prior_id


# ─── helpers ─────────────────────────────────────────────────────────────


async def runner_call_with_mode(
    runner_module: Any, *, mode: str, run_id: uuid.UUID,
) -> None:
    """Invoke ``_register_run`` with a custom ``run_mode`` value."""
    await runner_module._register_run(
        "postgresql://fake",
        str(run_id),
        "nt-trader-eth",
        "MACross-EMA-10-100",
        "ETH-USD-PERP.HYPERLIQUID",
        mode,
        {"ma_type": "EMA"},
    )
