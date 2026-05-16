"""Regression guard: balance-reading code paths must resolve their currency
from the account itself (``account.currencies()``) rather than hardcoding a
currency constant or asking the instrument.

We initially tried ``instrument.settlement_currency`` and
``instrument.get_cost_currency()`` (NT's documented "currency used for PnL"
API). Both failed on Hyperliquid:

    - HL reports settlement_currency=USDC (on-chain collateral) and the
      adapter loads the instrument with that. But the live HL exec client
      funds the simulated account in USD (quote currency, where PnL flows).
      ``balance(USDC)`` returns None against a USD-funded account.
    - NT's ``CryptoPerpetual.get_cost_currency()`` returns settlement_currency
      for non-inverse contracts, despite the docstring claiming it returns
      quote_currency. Same broken result on HL.

The honest source of truth is what's actually IN the account:
``account.currencies()`` returns the set of currencies the account has
balances in. For single-instrument deployments that's a singleton — the
currency we seeded with in ``run_sandbox.py`` / ``run_live.py``, which is
the same currency NT credits PnL to.

These tests are mechanical text scans. The recurrence patterns are:
    - hardcoded currency literal (e.g. ``balance(USDC)``)
    - instrument-derived currency in a balance call (works on Binance,
      breaks on HL)
Catching both via regex on balance(...) / balance_total(...) call sites.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

ACTOR_FILES = [
    REPO_ROOT / "src" / "actors" / "persistence.py",
    REPO_ROOT / "src" / "actors" / "alert.py",
]

# Strategy + mixin code paths that read account balance for sizing,
# liquidation, etc. — must use the same currency resolution as the actors.
BALANCE_READING_FILES = ACTOR_FILES + [
    REPO_ROOT / "src" / "strategies" / "ma_cross.py",
    REPO_ROOT / "src" / "core" / "liquidation_mixin.py",
]


@pytest.mark.parametrize("path", ACTOR_FILES, ids=lambda p: p.name)
def test_actor_does_not_import_specific_currency(path: Path) -> None:
    """Importing ``USDC`` (or any other specific currency constant) from
    ``nautilus_trader.model.currencies`` couples the actor to a single
    venue's settlement convention. Resolve from the instrument instead.
    """
    src = path.read_text(encoding="utf-8")
    forbidden = [
        "from nautilus_trader.model.currencies import",
        "from nautilus_trader.model.currencies import USDC",
        "from nautilus_trader.model.currencies import USDT",
        "from nautilus_trader.model.currencies import USD",
    ]
    for needle in forbidden:
        assert needle not in src, (
            f"{path.name} imports a specific currency constant ({needle!r}). "
            "Read instrument.settlement_currency at runtime instead — see "
            "PersistenceActor._persist_account_snapshot for the pattern."
        )


_BAD_BALANCE_CALL_RE = re.compile(
    r"\.balance(?:_total)?\("
    r"[^)]*("
    r"settlement_currency"
    r"|quote_currency"
    r"|get_cost_currency"
    r"|\bUSDC\b"
    r"|\bUSDT\b"
    r"|\bUSD\b"
    r"|\bBTC\b"
    r")[^)]*\)"
)


@pytest.mark.parametrize("path", BALANCE_READING_FILES, ids=lambda p: p.name)
def test_balance_lookups_resolve_from_account(path: Path) -> None:
    """Every site that reads an account balance must resolve the currency
    from the account itself (``account.currencies()``), not from a
    hardcoded literal or an instrument attribute. Catches recurrence of:

    * hardcoded currency literal (e.g. ``balance(USDC)``)
    * instrument-derived currency in a balance call:
        - ``settlement_currency``   — wrong on HL (returns USDC, account is USD)
        - ``quote_currency``        — works on HL, wrong on inverse contracts
        - ``get_cost_currency()``   — NT's docstring lies; on HL returns USDC

    The check is scoped to ``.balance(...)`` / ``.balance_total(...)`` call
    sites so docstrings and log lines that *mention* these attributes for
    observability are fine.
    """
    src = path.read_text(encoding="utf-8")
    assert "account.currencies()" in src, (
        f"{path.name} doesn't call account.currencies() to resolve the "
        "balance-lookup currency. See PersistenceActor._persist_account_snapshot."
    )
    bad_calls = _BAD_BALANCE_CALL_RE.findall(src)
    assert not bad_calls, (
        f"{path.name} has balance lookup(s) with the wrong currency source: "
        f"{bad_calls}. Resolve via account.currencies() instead — the module "
        "docstring explains why instrument-derived currencies fail on HL."
    )


def test_run_sandbox_seeds_starting_balance_in_usd_for_hyperliquid() -> None:
    """The sandbox runner seeds starting_balances in a currency that matches
    the venue's exec client convention. For Hyperliquid that's USD (see
    in-source comment). If we ever support a second venue here, this test
    will need to widen — but reintroducing 'USDC' would silently re-break
    the sandbox balance-tracking path.
    """
    src = (REPO_ROOT / "scripts" / "run_sandbox.py").read_text(encoding="utf-8")
    assert "starting_capital} USD\"" in src or "starting_capital} USD'" in src, (
        "run_sandbox.py must seed starting_balances in USD (HL adapter "
        "convention). 'USDC' here strands the balance — see the in-source "
        "comment for the full explanation."
    )
    assert "starting_capital} USDC\"" not in src and "starting_capital} USDC'" not in src, (
        "run_sandbox.py reverted to USDC starting balance — this re-introduces "
        "the HL stranded-balance bug. See the in-source comment."
    )
