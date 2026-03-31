"""Unit tests for src.core (constants + instruments)."""

from decimal import Decimal

from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import CryptoPerpetual

from src.core.constants import (
    HL_CANDLE_LIMIT,
    HYPERLIQUID_API_URL,
    HYPERLIQUID_VENUE,
    INTERVAL_TO_BAR_SPEC,
    MAKER_FEE,
    SETTLEMENT_CURRENCY,
    TAKER_FEE,
    TS_INIT_DELTAS,
)
from src.core.instruments import make_hyperliquid_perp, with_venue_config

# --- constants ---


class TestConstants:
    def test_venue_type_and_value(self) -> None:
        assert isinstance(HYPERLIQUID_VENUE, Venue)
        assert str(HYPERLIQUID_VENUE) == "HYPERLIQUID"

    def test_api_url(self) -> None:
        assert HYPERLIQUID_API_URL.startswith("https://")

    def test_fees_are_decimal(self) -> None:
        assert isinstance(MAKER_FEE, Decimal)
        assert isinstance(TAKER_FEE, Decimal)

    def test_fees_positive_and_less_than_one(self) -> None:
        assert Decimal(0) < MAKER_FEE < Decimal(1)
        assert Decimal(0) < TAKER_FEE < Decimal(1)

    def test_maker_less_than_taker(self) -> None:
        assert MAKER_FEE < TAKER_FEE

    def test_settlement_currency_is_usdc(self) -> None:
        assert SETTLEMENT_CURRENCY == USDC

    def test_hl_candle_limit(self) -> None:
        assert HL_CANDLE_LIMIT == 5000

    def test_interval_to_bar_spec_keys(self) -> None:
        assert "1h" in INTERVAL_TO_BAR_SPEC
        assert "4h" in INTERVAL_TO_BAR_SPEC
        assert "1d" in INTERVAL_TO_BAR_SPEC

    def test_interval_to_bar_spec_values(self) -> None:
        step, agg = INTERVAL_TO_BAR_SPEC["1h"]
        assert step == 1
        assert agg == "HOUR"

    def test_ts_init_deltas_match_intervals(self) -> None:
        assert set(TS_INIT_DELTAS.keys()) == set(INTERVAL_TO_BAR_SPEC.keys())

    def test_ts_init_deltas_are_positive_ints(self) -> None:
        for key, val in TS_INIT_DELTAS.items():
            assert isinstance(val, int), f"{key} delta is not int"
            assert val > 0, f"{key} delta is not positive"

    def test_ts_init_delta_1h_value(self) -> None:
        assert TS_INIT_DELTAS["1h"] == 3_600_000_000_000  # 1 hour in nanoseconds


# --- instruments ---


class TestMakeHyperliquidPerp:
    def test_returns_crypto_perpetual(self) -> None:
        inst = make_hyperliquid_perp("BTC", 1, 5, 40)
        assert isinstance(inst, CryptoPerpetual)

    def test_instrument_id(self) -> None:
        inst = make_hyperliquid_perp("BTC", 1, 5, 40)
        assert str(inst.id) == "BTC-USD-PERP.HYPERLIQUID"

    def test_raw_symbol(self) -> None:
        inst = make_hyperliquid_perp("BTC", 1, 5, 40)
        assert str(inst.raw_symbol) == "BTC"

    def test_settlement_currency(self) -> None:
        inst = make_hyperliquid_perp("BTC", 1, 5, 40)
        assert inst.settlement_currency == USDC

    def test_is_not_inverse(self) -> None:
        inst = make_hyperliquid_perp("BTC", 1, 5, 40)
        assert inst.is_inverse is False

    def test_margin_init_leverage_40(self) -> None:
        inst = make_hyperliquid_perp("BTC", 1, 5, 40)
        assert inst.margin_init == Decimal("0.025")  # 1/40

    def test_margin_maint_half_of_init(self) -> None:
        inst = make_hyperliquid_perp("BTC", 1, 5, 40)
        assert inst.margin_maint == Decimal("0.0125")

    def test_price_precision(self) -> None:
        inst = make_hyperliquid_perp("BTC", 1, 5, 40)
        assert inst.price_precision == 1

    def test_size_precision(self) -> None:
        inst = make_hyperliquid_perp("BTC", 1, 5, 40)
        assert inst.size_precision == 5

    def test_price_increment(self) -> None:
        inst = make_hyperliquid_perp("BTC", 1, 5, 40)
        assert str(inst.price_increment) == "0.1"

    def test_size_increment(self) -> None:
        inst = make_hyperliquid_perp("BTC", 1, 5, 40)
        assert str(inst.size_increment) == "0.00001"

    def test_make_qty_roundtrip(self) -> None:
        inst = make_hyperliquid_perp("BTC", 1, 5, 40)
        qty = inst.make_qty(Decimal("0.01000"))
        assert str(qty) == "0.01000"

    def test_eth_instrument_id(self) -> None:
        inst = make_hyperliquid_perp("ETH", 2, 4, 25)
        assert str(inst.id) == "ETH-USD-PERP.HYPERLIQUID"
        assert inst.price_precision == 2
        assert inst.size_precision == 4

    def test_sol_instrument_id(self) -> None:
        inst = make_hyperliquid_perp("SOL", 3, 2, 20)
        assert str(inst.id) == "SOL-USD-PERP.HYPERLIQUID"
        assert inst.margin_init == Decimal("0.05")  # 1/20

    def test_custom_fees(self) -> None:
        inst = make_hyperliquid_perp(
            "BTC", 1, 4, 50,
            maker_fee=Decimal("0.00005"),
            taker_fee=Decimal("0.00020"),
        )
        assert inst.maker_fee == Decimal("0.00005")
        assert inst.taker_fee == Decimal("0.00020")


# --- with_venue_config ---


class TestWithVenueConfig:
    def test_margin_init(self) -> None:
        base = make_hyperliquid_perp("BTC", 1, 5, 40)
        result = with_venue_config(base, 20)
        assert result.margin_init == Decimal("0.05")  # 1/20

    def test_margin_maint(self) -> None:
        base = make_hyperliquid_perp("BTC", 1, 5, 40)
        result = with_venue_config(base, 20)
        assert result.margin_maint == Decimal("0.025")  # 0.05/2

    def test_preserves_fees_by_default(self) -> None:
        base = make_hyperliquid_perp("BTC", 1, 5, 40)
        result = with_venue_config(base, 20)
        assert result.maker_fee == base.maker_fee
        assert result.taker_fee == base.taker_fee

    def test_override_both_fees(self) -> None:
        base = make_hyperliquid_perp("BTC", 1, 5, 40)
        result = with_venue_config(
            base, 20,
            maker_fee=Decimal("0.001"),
            taker_fee=Decimal("0.002"),
        )
        assert result.maker_fee == Decimal("0.001")
        assert result.taker_fee == Decimal("0.002")

    def test_override_maker_only(self) -> None:
        base = make_hyperliquid_perp("BTC", 1, 5, 40)
        result = with_venue_config(base, 20, maker_fee=Decimal("0.0005"))
        assert result.maker_fee == Decimal("0.0005")
        assert result.taker_fee == base.taker_fee

    def test_override_taker_only(self) -> None:
        base = make_hyperliquid_perp("BTC", 1, 5, 40)
        result = with_venue_config(base, 20, taker_fee=Decimal("0.003"))
        assert result.maker_fee == base.maker_fee
        assert result.taker_fee == Decimal("0.003")

    def test_preserves_instrument_id(self) -> None:
        base = make_hyperliquid_perp("BTC", 1, 5, 40)
        result = with_venue_config(base, 10)
        assert result.id == base.id

    def test_preserves_precision(self) -> None:
        base = make_hyperliquid_perp("ETH", 2, 4, 25)
        result = with_venue_config(base, 10)
        assert result.price_precision == 2
        assert result.size_precision == 4
