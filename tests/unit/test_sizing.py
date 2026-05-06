"""Unit tests for src.core.sizing."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from src.core.sizing import (
    SizingConfig,
    compute_notional,
    resolve_min_trade_notional,
    resolve_sizing_from_strategy_config,
)

# ── Test fixtures ──────────────────────────────────────────────────────────


class _FakeInstrument:
    """Minimal Instrument stand-in with a min_notional attribute.

    ``compute_notional`` reads ``instrument.min_notional`` via ``getattr``;
    using a real NT Instrument here would pull in the whole engine.
    """

    def __init__(self, min_notional: Decimal | None = None) -> None:
        self.min_notional = min_notional


# ── SizingConfig ───────────────────────────────────────────────────────────


class TestSizingConfig:
    def test_fixed_mode(self) -> None:
        cfg = SizingConfig(mode="fixed", fixed_notional=Decimal("500"))
        assert cfg.mode == "fixed"
        assert cfg.fixed_notional == Decimal("500")
        assert cfg.risk_frac is None
        assert cfg.stop_pct is None
        assert cfg.min_notional is None

    def test_equity_frac_mode(self) -> None:
        cfg = SizingConfig(
            mode="equity_frac",
            risk_frac=Decimal("0.10"),
            stop_pct=Decimal("0.05"),
            min_notional=Decimal("50"),
        )
        assert cfg.mode == "equity_frac"
        assert cfg.risk_frac == Decimal("0.10")
        assert cfg.stop_pct == Decimal("0.05")
        assert cfg.min_notional == Decimal("50")


# ── compute_notional ───────────────────────────────────────────────────────


class TestComputeNotionalFixed:
    def test_returns_fixed_notional(self) -> None:
        cfg = SizingConfig(mode="fixed", fixed_notional=Decimal("500"))
        result = compute_notional(
            equity=Decimal("1000"),
            cfg=cfg,
            instrument=_FakeInstrument(),
        )
        assert result == Decimal("500")

    def test_floored_at_instrument_min(self) -> None:
        """fixed_notional below instrument floor → instrument floor wins."""
        cfg = SizingConfig(mode="fixed", fixed_notional=Decimal("5"))
        result = compute_notional(
            equity=Decimal("1000"),
            cfg=cfg,
            instrument=_FakeInstrument(min_notional=Decimal("10")),
        )
        assert result == Decimal("10")

    def test_floored_at_config_min(self) -> None:
        """fixed_notional below config floor → config floor wins."""
        cfg = SizingConfig(
            mode="fixed",
            fixed_notional=Decimal("5"),
            min_notional=Decimal("100"),
        )
        result = compute_notional(
            equity=Decimal("1000"),
            cfg=cfg,
            instrument=_FakeInstrument(min_notional=Decimal("10")),
        )
        assert result == Decimal("100")

    def test_raises_when_fixed_notional_missing(self) -> None:
        cfg = SizingConfig(mode="fixed")
        with pytest.raises(ValueError, match="positive fixed_notional"):
            compute_notional(
                equity=Decimal("1000"),
                cfg=cfg,
                instrument=_FakeInstrument(),
            )


class TestComputeNotionalEquityFrac:
    def test_target_model(self) -> None:
        """$1000 equity, 10% risk, 5% stop → $2000 notional."""
        cfg = SizingConfig(
            mode="equity_frac",
            risk_frac=Decimal("0.10"),
            stop_pct=Decimal("0.05"),
        )
        result = compute_notional(
            equity=Decimal("1000"),
            cfg=cfg,
            instrument=_FakeInstrument(),
        )
        assert result == Decimal("2000")

    def test_scales_with_equity(self) -> None:
        """Drawdown halves equity → notional halves."""
        cfg = SizingConfig(
            mode="equity_frac",
            risk_frac=Decimal("0.10"),
            stop_pct=Decimal("0.05"),
        )
        full = compute_notional(Decimal("1000"), cfg, _FakeInstrument())
        half = compute_notional(Decimal("500"), cfg, _FakeInstrument())
        assert full == Decimal("2000")
        assert half == Decimal("1000")

    def test_floor_binds_at_low_equity(self) -> None:
        """Below the floor-binding equity, notional pins to min_notional."""
        cfg = SizingConfig(
            mode="equity_frac",
            risk_frac=Decimal("0.10"),
            stop_pct=Decimal("0.05"),
            min_notional=Decimal("50"),
        )
        # Equity at 25 → raw = (0.10 * 25) / 0.05 = 50, floor = 50, result = 50
        # Equity at 10 → raw = 20, floor = 50 → result = 50 (floor binds)
        assert compute_notional(Decimal("25"), cfg, _FakeInstrument()) == Decimal("50")
        assert compute_notional(Decimal("10"), cfg, _FakeInstrument()) == Decimal("50")

    def test_zero_equity_zero_notional(self) -> None:
        """Equity ≤ 0 → notional 0 (caller skips submission)."""
        cfg = SizingConfig(
            mode="equity_frac",
            risk_frac=Decimal("0.10"),
            stop_pct=Decimal("0.05"),
        )
        assert compute_notional(
            Decimal("0"), cfg, _FakeInstrument(),
        ) == Decimal("0")

    def test_raises_when_risk_frac_missing(self) -> None:
        cfg = SizingConfig(mode="equity_frac", stop_pct=Decimal("0.05"))
        with pytest.raises(ValueError, match="positive risk_frac"):
            compute_notional(Decimal("1000"), cfg, _FakeInstrument())

    def test_raises_when_stop_pct_missing(self) -> None:
        cfg = SizingConfig(mode="equity_frac", risk_frac=Decimal("0.10"))
        with pytest.raises(ValueError, match="positive stop_pct"):
            compute_notional(Decimal("1000"), cfg, _FakeInstrument())


# ── resolve_min_trade_notional ─────────────────────────────────────────────


class TestResolveMinTradeNotional:
    def test_explicit_override_wins(self) -> None:
        result = resolve_min_trade_notional(
            sizing=SizingConfig(mode="fixed", fixed_notional=Decimal("500")),
            instrument=_FakeInstrument(min_notional=Decimal("10")),
            explicit=Decimal("200"),
        )
        assert result == Decimal("200")

    def test_sizing_min_notional_second(self) -> None:
        result = resolve_min_trade_notional(
            sizing=SizingConfig(
                mode="equity_frac",
                risk_frac=Decimal("0.10"),
                stop_pct=Decimal("0.05"),
                min_notional=Decimal("50"),
            ),
            instrument=_FakeInstrument(min_notional=Decimal("10")),
            explicit=None,
        )
        assert result == Decimal("50")

    def test_sizing_fixed_notional_third(self) -> None:
        """Fixed mode without min_notional → fall through to fixed_notional."""
        result = resolve_min_trade_notional(
            sizing=SizingConfig(mode="fixed", fixed_notional=Decimal("500")),
            instrument=_FakeInstrument(min_notional=Decimal("10")),
            explicit=None,
        )
        assert result == Decimal("500")

    def test_instrument_min_notional_fourth(self) -> None:
        result = resolve_min_trade_notional(
            sizing=None,
            instrument=_FakeInstrument(min_notional=Decimal("10")),
            explicit=None,
        )
        assert result == Decimal("10")

    def test_raises_when_no_source(self) -> None:
        with pytest.raises(ValueError, match="Cannot resolve min_trade_notional"):
            resolve_min_trade_notional(
                sizing=None,
                instrument=_FakeInstrument(min_notional=None),
                explicit=None,
            )

    def test_raises_with_only_zero_sources(self) -> None:
        """Zero values are treated as not-set."""
        with pytest.raises(ValueError, match="Cannot resolve min_trade_notional"):
            resolve_min_trade_notional(
                sizing=SizingConfig(
                    mode="fixed",
                    fixed_notional=Decimal("0"),
                    min_notional=Decimal("0"),
                ),
                instrument=_FakeInstrument(min_notional=Decimal("0")),
                explicit=Decimal("0"),
            )


# ── resolve_sizing_from_strategy_config ────────────────────────────────────


class _ConfigWithSizing:
    """Stand-in for a strategy config with explicit ``sizing``."""

    def __init__(self, sizing: SizingConfig) -> None:
        self.sizing = sizing


class _ConfigWithTradeNotional:
    """Stand-in for a strategy config with back-compat ``trade_notional``."""

    def __init__(self, trade_notional: Decimal) -> None:
        self.sizing: Any = None
        self.trade_notional = trade_notional


class _ConfigEmpty:
    """Stand-in with neither field set."""

    sizing: Any = None
    trade_notional: Any = None


class TestResolveSizingFromStrategyConfig:
    def test_explicit_sizing_wins(self) -> None:
        sizing = SizingConfig(mode="fixed", fixed_notional=Decimal("500"))
        result = resolve_sizing_from_strategy_config(_ConfigWithSizing(sizing))
        assert result is sizing  # same object

    def test_trade_notional_back_compat(self) -> None:
        cfg = _ConfigWithTradeNotional(Decimal("250"))
        result = resolve_sizing_from_strategy_config(cfg)
        assert result.mode == "fixed"
        assert result.fixed_notional == Decimal("250")

    def test_raises_when_neither_set(self) -> None:
        with pytest.raises(ValueError, match="requires either"):
            resolve_sizing_from_strategy_config(_ConfigEmpty())

    def test_raises_on_zero_trade_notional(self) -> None:
        cfg = _ConfigWithTradeNotional(Decimal("0"))
        with pytest.raises(ValueError, match="requires either"):
            resolve_sizing_from_strategy_config(cfg)
