"""Tests for the close_reason derivation pipeline:

1. ``_close_reason_from_tags`` (the pure helper) returns the right string
   for each tag combination and falls back to ``'unknown'`` otherwise.
2. Regression scan that the four recognised tag strings stay in lockstep
   between persistence.py (where they're hardcoded for module-boundary
   reasons — see CLAUDE.md "Module Dependency Direction") and the mixin /
   strategy modules where they originate.

The actor's ``_derive_close_reason`` is a thin wrapper over the helper +
an ``self.cache.order()`` lookup. Mocking NT's Cython ``Actor.cache``
getset_descriptor is not portable across CPython versions, so we test
the pure helper directly and rely on the regression scans + manual
verification on the live droplet for the cache-lookup half.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── _close_reason_from_tags behaviour ──────────────────────────────────────


@pytest.mark.parametrize(
    "tags, expected",
    [
        (["protective_stop"],          "protective_stop"),
        (["liquidation"],              "liquidation"),
        (["strategy_exit"],            "strategy_exit"),
        (["shutdown_flatten"],         "shutdown_flatten"),
        (["protective_stop", "extra"], "protective_stop"),
        (["unrelated_tag"],            "unknown"),
        ([],                           "unknown"),
        (None,                         "unknown"),
    ],
)
def test_close_reason_from_tags(tags: list[str] | None, expected: str) -> None:
    """Each recognised tag resolves to the matching reason; everything
    else (empty list, None, or only unrecognised tags) falls back to
    'unknown'.
    """
    from src.actors.persistence import _close_reason_from_tags
    assert _close_reason_from_tags(tags) == expected


def test_close_reason_first_recognised_wins() -> None:
    """If an order somehow carries multiple recognised tags, the function
    returns the first one in priority order (protective_stop > liquidation
    > strategy_exit > shutdown_flatten). Documenting current behaviour;
    in practice an order should only ever carry one of these.
    """
    from src.actors.persistence import _close_reason_from_tags
    assert _close_reason_from_tags(["liquidation", "protective_stop"]) == "protective_stop"


# ── Tag-string lockstep regression scan ────────────────────────────────────


_TAG_STRINGS = {
    "protective_stop":  ("src/core/protective_stop_mixin.py", "PROTECTIVE_STOP_TAG"),
    "liquidation":      ("src/core/liquidation_mixin.py",     "LIQUIDATION_TAG"),
    "strategy_exit":    ("src/strategies/ma_cross.py",        "STRATEGY_EXIT_TAG"),
    "shutdown_flatten": ("src/strategies/ma_cross.py",        "SHUTDOWN_FLATTEN_TAG"),
}


@pytest.mark.parametrize("tag_string, source_loc", list(_TAG_STRINGS.items()))
def test_tag_string_is_defined_in_source(tag_string: str, source_loc: tuple[str, str]) -> None:
    """Each tag string PersistenceActor checks must be defined as a constant
    in the corresponding mixin/strategy file. Catches drift where someone
    renames the tag in one place but not the other.
    """
    path, const_name = source_loc
    src = (REPO_ROOT / path).read_text(encoding="utf-8")
    # Look for ``CONST_NAME = "tag_string"`` (with single or double quotes)
    pattern = rf'{re.escape(const_name)}\s*=\s*["\']({re.escape(tag_string)})["\']'
    assert re.search(pattern, src), (
        f"{path} doesn't define {const_name} = \"{tag_string}\". "
        f"PersistenceActor._derive_close_reason hardcodes \"{tag_string}\" — "
        "if you rename the constant, update both places (see CLAUDE.md "
        "'Module Dependency Direction' for why the actor doesn't import "
        "from strategies/)."
    )


def test_persistence_actor_checks_every_known_tag() -> None:
    """PersistenceActor._derive_close_reason must reference every tag string
    listed in _TAG_STRINGS. Catches the case where someone adds a new tag
    constant but forgets to wire it into the actor.
    """
    src = (REPO_ROOT / "src" / "actors" / "persistence.py").read_text(encoding="utf-8")
    for tag_string in _TAG_STRINGS:
        assert f'"{tag_string}"' in src, (
            f"PersistenceActor doesn't check \"{tag_string}\". Either add a "
            f'branch to ``_derive_close_reason`` or remove the constant from '
            f"{_TAG_STRINGS[tag_string][0]}."
        )


def test_macross_passes_strategy_exit_tag_to_close_all_positions() -> None:
    """The strategy's cross-flip exit must call ``close_all_positions`` with
    ``tags=[STRATEGY_EXIT_TAG]``; otherwise the closing order has no tag
    and PersistenceActor records ``'unknown'`` for what should be ``'strategy_exit'``.
    """
    src = (REPO_ROOT / "src" / "strategies" / "ma_cross.py").read_text(encoding="utf-8")
    assert "tags=[STRATEGY_EXIT_TAG]" in src, (
        "MACross cross-flip exit must pass tags=[STRATEGY_EXIT_TAG] to "
        "close_all_positions so PersistenceActor can record 'strategy_exit'."
    )


def test_macross_passes_shutdown_flatten_tag_in_on_stop() -> None:
    """The on_stop handler's close_all_positions call must use SHUTDOWN_FLATTEN_TAG
    to distinguish lifecycle-driven flattens from deliberate cross-flip exits.
    """
    src = (REPO_ROOT / "src" / "strategies" / "ma_cross.py").read_text(encoding="utf-8")
    assert "tags=[SHUTDOWN_FLATTEN_TAG]" in src, (
        "MACross.on_stop must pass tags=[SHUTDOWN_FLATTEN_TAG] to "
        "close_all_positions so PersistenceActor can record 'shutdown_flatten'."
    )
