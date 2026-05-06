"""Unit tests for liquidation formulas and config."""

from decimal import Decimal

from nautilus_trader.core.rust.model import PositionSide

from src.core.liquidation import (
    AccountLiquidated,
    LiquidationConfig,
    PositionLiquidated,
    compute_liquidation_price,
    is_account_alive,
)

# --- LiquidationConfig ---


class TestLiquidationConfig:
    def test_defaults(self) -> None:
        cfg = LiquidationConfig()
        assert cfg.enabled is True
        # All resolution-order-driven fields default to None so make_engine
        # can fill them from VenueConfig / SizingConfig / instrument.
        assert cfg.mm_rate is None
        assert cfg.fee_rate is None
        assert cfg.min_trade_notional is None
        assert cfg.alive_trades_buffer == 1
        assert cfg.halt_on_account_liquidation is True

    def test_custom_values(self) -> None:
        cfg = LiquidationConfig(
            enabled=False,
            mm_rate=Decimal("0.004"),
            fee_rate=Decimal("0.0005"),
            min_trade_notional=Decimal("100"),
            alive_trades_buffer=2,
            halt_on_account_liquidation=False,
        )
        assert cfg.enabled is False
        assert cfg.mm_rate == Decimal("0.004")
        assert cfg.fee_rate == Decimal("0.0005")
        assert cfg.min_trade_notional == Decimal("100")
        assert cfg.alive_trades_buffer == 2
        assert cfg.halt_on_account_liquidation is False


# --- Event types ---


class TestPositionLiquidated:
    def test_fields_no_slippage(self) -> None:
        """Trigger and fill match — perfectly clean liquidation."""
        event = PositionLiquidated(
            instrument_id="BTC-USD-PERP.HYPERLIQUID",
            side=PositionSide.LONG,
            entry_price=Decimal("50000"),
            trigger_price=Decimal("25250"),
            fill_price=Decimal("25250"),
            realized_pnl=Decimal("-990"),
            ts_event=1000000000,
        )
        assert event.instrument_id == "BTC-USD-PERP.HYPERLIQUID"
        assert event.side == PositionSide.LONG
        assert event.entry_price == Decimal("50000")
        assert event.trigger_price == Decimal("25250")
        assert event.fill_price == Decimal("25250")
        assert event.fill_price - event.trigger_price == Decimal("0")
        assert event.realized_pnl == Decimal("-990")
        assert event.ts_event == 1000000000

    def test_fields_with_slippage(self) -> None:
        """Trigger and fill differ — bar-decomposition gap risk captured."""
        event = PositionLiquidated(
            instrument_id="BTC-USD-PERP.HYPERLIQUID",
            side=PositionSide.LONG,
            entry_price=Decimal("50000"),
            trigger_price=Decimal("25250"),     # mixin set the stop here
            fill_price=Decimal("23000"),         # bar's L gapped through the trigger
            realized_pnl=Decimal("-1080"),       # worse than expected -990
            ts_event=1000000000,
        )
        # Slippage = how much worse the fill was vs the trigger
        slippage = event.trigger_price - event.fill_price
        assert slippage == Decimal("2250")


class TestAccountLiquidated:
    def test_fields(self) -> None:
        event = AccountLiquidated(
            equity=Decimal("4.50"),
            required=Decimal("5.10"),
            ts_event=2000000000,
        )
        assert event.equity == Decimal("4.50")
        assert event.required == Decimal("5.10")
        assert event.ts_event == 2000000000


# --- compute_liquidation_price ---


class TestComputeLiquidationPrice:
    def test_long_target_model(self) -> None:
        """Target model: $1000 equity, $2000 notional, 0.5% mm_rate.

        liq_distance = 1000/2000 - 0.005 = 0.495
        liq_price = 50000 * (1 - 0.495) = 50000 * 0.505 = 25250
        """
        result = compute_liquidation_price(
            entry_price=Decimal("50000"),
            side=PositionSide.LONG,
            equity=Decimal("1000"),
            notional=Decimal("2000"),
            mm_rate=Decimal("0.005"),
        )
        assert result == Decimal("50000") * Decimal("0.505")

    def test_short_target_model(self) -> None:
        """Short position: liq_price = entry * (1 + liq_distance).

        liq_distance = 0.495
        liq_price = 50000 * 1.495 = 74750
        """
        result = compute_liquidation_price(
            entry_price=Decimal("50000"),
            side=PositionSide.SHORT,
            equity=Decimal("1000"),
            notional=Decimal("2000"),
            mm_rate=Decimal("0.005"),
        )
        assert result == Decimal("50000") * Decimal("1.495")

    def test_current_sweep_config(self) -> None:
        """Current sweep: $100 equity, $500 notional, 0.5% mm_rate.

        liq_distance = 100/500 - 0.005 = 0.195
        liq_price = 50000 * (1 - 0.195) = 50000 * 0.805 = 40250
        ~19.5% adverse excursion.
        """
        result = compute_liquidation_price(
            entry_price=Decimal("50000"),
            side=PositionSide.LONG,
            equity=Decimal("100"),
            notional=Decimal("500"),
            mm_rate=Decimal("0.005"),
        )
        assert result == Decimal("50000") * Decimal("0.805")

    def test_max_gross_leverage(self) -> None:
        """Max gross leverage: $100 equity, $2000 notional.

        liq_distance = 100/2000 - 0.005 = 0.045
        liq_price = 50000 * (1 - 0.045) = 50000 * 0.955 = 47750
        ~4.5% adverse excursion.
        """
        result = compute_liquidation_price(
            entry_price=Decimal("50000"),
            side=PositionSide.LONG,
            equity=Decimal("100"),
            notional=Decimal("2000"),
            mm_rate=Decimal("0.005"),
        )
        assert result == Decimal("50000") * Decimal("0.955")

    def test_zero_liq_distance(self) -> None:
        """equity = notional * mm_rate → liq_distance = 0 → liquidation at entry."""
        result = compute_liquidation_price(
            entry_price=Decimal("50000"),
            side=PositionSide.LONG,
            equity=Decimal("10"),       # 2000 * 0.005
            notional=Decimal("2000"),
            mm_rate=Decimal("0.005"),
        )
        assert result == Decimal("50000")

    def test_negative_liq_distance(self) -> None:
        """equity < notional * mm_rate → already past liquidation.

        liq_distance = 5/2000 - 0.005 = -0.0025
        liq_price = 50000 * (1 + 0.0025) = 50125 (above entry for a long)
        """
        result = compute_liquidation_price(
            entry_price=Decimal("50000"),
            side=PositionSide.LONG,
            equity=Decimal("5"),
            notional=Decimal("2000"),
            mm_rate=Decimal("0.005"),
        )
        assert result > Decimal("50000")  # above entry = already past liquidation


# --- is_account_alive ---


class TestIsAccountAlive:
    def test_alive_with_margin(self) -> None:
        """$100 equity, $100 min_notional, 20x → floor_im=$5, fees=$0.10."""
        assert is_account_alive(
            equity=Decimal("100"),
            min_trade_notional=Decimal("100"),
            venue_leverage=20,
            fee_rate=Decimal("0.0005"),
        ) is True

    def test_dead_insufficient_equity(self) -> None:
        """$4 equity can't cover $5 IM + $0.10 fees = $5.10."""
        assert is_account_alive(
            equity=Decimal("4"),
            min_trade_notional=Decimal("100"),
            venue_leverage=20,
            fee_rate=Decimal("0.0005"),
        ) is False

    def test_alive_at_exact_threshold(self) -> None:
        """Equity exactly at threshold: $5.10 = $5 IM + $0.10 fees."""
        assert is_account_alive(
            equity=Decimal("5.10"),
            min_trade_notional=Decimal("100"),
            venue_leverage=20,
            fee_rate=Decimal("0.0005"),
        ) is True

    def test_dead_just_below_threshold(self) -> None:
        """$5.09 < $5.10 threshold."""
        assert is_account_alive(
            equity=Decimal("5.09"),
            min_trade_notional=Decimal("100"),
            venue_leverage=20,
            fee_rate=Decimal("0.0005"),
        ) is False

    def test_buffer_doubles_fee_requirement(self) -> None:
        """alive_trades_buffer=2 → fee_buffer doubles.

        floor_im = $5, fee_buffer = $100 * 0.0005 * 2 * 2 = $0.20
        threshold = $5.20
        """
        assert is_account_alive(
            equity=Decimal("5.20"),
            min_trade_notional=Decimal("100"),
            venue_leverage=20,
            fee_rate=Decimal("0.0005"),
            alive_trades_buffer=2,
        ) is True

        assert is_account_alive(
            equity=Decimal("5.19"),
            min_trade_notional=Decimal("100"),
            venue_leverage=20,
            fee_rate=Decimal("0.0005"),
            alive_trades_buffer=2,
        ) is False

    def test_no_leverage(self) -> None:
        """Spot (leverage=1): floor_im = full notional.

        floor_im = $100, fee_buffer = $100 * 0.001 * 2 = $0.20
        threshold = $100.20
        """
        assert is_account_alive(
            equity=Decimal("100.20"),
            min_trade_notional=Decimal("100"),
            venue_leverage=1,
            fee_rate=Decimal("0.001"),
        ) is True

        assert is_account_alive(
            equity=Decimal("100.19"),
            min_trade_notional=Decimal("100"),
            venue_leverage=1,
            fee_rate=Decimal("0.001"),
        ) is False
