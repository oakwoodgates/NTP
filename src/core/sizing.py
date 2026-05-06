"""Equity-aware position sizing.

Two modes:

- ``mode="fixed"`` — every entry submits ``fixed_notional``. Backwards-
  compatible with the old ``trade_notional`` pattern.
- ``mode="equity_frac"`` — every entry submits ``(risk_frac × equity) / stop_pct``,
  floored at ``max(min_notional, instrument.min_notional)``.

Rationale for the equity-fraction formula:

- ``risk_frac × equity`` is the **risk budget** (1R, in USD).
- Dividing by ``stop_pct`` converts the risk budget into a notional. With
  a protective stop placed at ``entry × (1 − stop_pct)`` (long), filling
  the stop loses exactly the risk budget.
- The form is deliberately leverage-independent: sizing is a function of
  equity and stop distance only. ``LEVERAGE`` affects what's *feasible*
  (initial margin) and where liquidation sits, not the notional.

``LiquidationConfig.min_trade_notional`` resolves from ``SizingConfig.min_notional``
(if set) or ``SizingConfig.fixed_notional`` (in fixed mode) when not
explicitly overridden — keeping the death-floor and the sizing-floor in sync.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from nautilus_trader.common.config import NautilusConfig

if TYPE_CHECKING:
    from nautilus_trader.model.instruments import Instrument


class SizingConfig(NautilusConfig, frozen=True):
    """Declarative position sizing.

    Attributes
    ----------
    mode : Literal["fixed", "equity_frac"]
        Sizing mode. ``"fixed"`` uses ``fixed_notional`` directly;
        ``"equity_frac"`` derives notional from equity and stop distance.
    fixed_notional : Decimal | None
        Required when ``mode == "fixed"``.  Notional USD per entry.
    risk_frac : Decimal | None
        Required when ``mode == "equity_frac"``.  Fraction of equity to
        risk per trade (e.g. ``Decimal("0.10")`` for 10%).
    stop_pct : Decimal | None
        Required when ``mode == "equity_frac"``.  Protective stop distance
        as a fraction of entry price (e.g. ``Decimal("0.05")`` for 5%).
        Notional = ``(risk_frac × equity) / stop_pct``.
    min_notional : Decimal | None
        Optional floor on the computed notional.  Also feeds
        ``LiquidationConfig.min_trade_notional`` when that is unset, so
        the death-floor matches the sizing-floor.

    """

    mode: Literal["fixed", "equity_frac"]
    fixed_notional: Decimal | None = None
    risk_frac: Decimal | None = None
    stop_pct: Decimal | None = None
    min_notional: Decimal | None = None


def compute_notional(
    equity: Decimal,
    cfg: SizingConfig,
    instrument: Instrument,
) -> Decimal:
    """Return the notional USD size for the next entry.

    Parameters
    ----------
    equity
        Account equity (e.g. ``account.balance_total(currency).as_decimal()``).
    cfg
        Sizing configuration.
    instrument
        Used to apply the venue's minimum notional as a floor.

    Returns
    -------
    Decimal
        Notional, floored at ``max(cfg.min_notional, instrument.min_notional)``.

    Raises
    ------
    ValueError
        If required fields for the chosen mode are missing or non-positive.

    """
    if cfg.mode == "fixed":
        if cfg.fixed_notional is None or cfg.fixed_notional <= 0:
            msg = "SizingConfig(mode='fixed') requires positive fixed_notional"
            raise ValueError(msg)
        raw = cfg.fixed_notional
    else:  # equity_frac
        if cfg.risk_frac is None or cfg.risk_frac <= 0:
            msg = "SizingConfig(mode='equity_frac') requires positive risk_frac"
            raise ValueError(msg)
        if cfg.stop_pct is None or cfg.stop_pct <= 0:
            msg = "SizingConfig(mode='equity_frac') requires positive stop_pct"
            raise ValueError(msg)
        # Negative or zero equity → zero notional (caller guards on this).
        # Don't raise here; the strategy's _enter() guard will skip submission.
        raw = (cfg.risk_frac * equity) / cfg.stop_pct if equity > 0 else Decimal("0")

    instrument_min = _instrument_min_notional(instrument)
    floor = _max_decimal(cfg.min_notional, instrument_min)
    return _max_decimal(raw, floor)


def resolve_min_trade_notional(
    sizing: SizingConfig | None,
    instrument: Instrument | None,
    explicit: Decimal | None = None,
) -> Decimal:
    """Resolve the account-alive predicate's ``min_trade_notional`` floor.

    Resolution order (first non-None wins):

    1. ``explicit`` — caller-provided override (typically
       ``LiquidationConfig.min_trade_notional`` set by the user).
    2. ``sizing.min_notional`` — sizing config's explicit floor.
    3. ``sizing.fixed_notional`` — when ``sizing.mode == "fixed"``.
    4. ``instrument.min_notional`` — venue minimum.

    Raises
    ------
    ValueError
        When no source is available.

    """
    if explicit is not None and explicit > 0:
        return explicit
    if sizing is not None:
        if sizing.min_notional is not None and sizing.min_notional > 0:
            return sizing.min_notional
        if (
            sizing.mode == "fixed"
            and sizing.fixed_notional is not None
            and sizing.fixed_notional > 0
        ):
            return sizing.fixed_notional
    if instrument is not None:
        instrument_min = _instrument_min_notional(instrument)
        if instrument_min > 0:
            return instrument_min
    msg = (
        "Cannot resolve min_trade_notional. Provide an explicit value on "
        "LiquidationConfig.min_trade_notional, set SizingConfig.min_notional "
        "or SizingConfig.fixed_notional, or use an instrument that exposes a "
        "venue minimum notional."
    )
    raise ValueError(msg)


def resolve_sizing_from_strategy_config(config: object) -> SizingConfig:
    """Extract a ``SizingConfig`` from a strategy's config object.

    Resolution order:

    1. ``config.sizing`` if set.
    2. ``config.trade_notional`` (back-compat) → ``SizingConfig(mode="fixed", ...)``.

    Raises ``ValueError`` when neither is set.

    Each migrated strategy calls this once in its ``__init__`` to populate
    ``self._sizing``.
    """
    sizing = getattr(config, "sizing", None)
    if isinstance(sizing, SizingConfig):
        return sizing
    trade_notional = getattr(config, "trade_notional", None)
    if (
        trade_notional is not None
        and isinstance(trade_notional, Decimal)
        and trade_notional > 0
    ):
        return SizingConfig(mode="fixed", fixed_notional=trade_notional)
    msg = (
        "Strategy config requires either a `sizing: SizingConfig` field or "
        "a positive `trade_notional: Decimal` field. See SizingConfig docs."
    )
    raise ValueError(msg)


def _instrument_min_notional(instrument: Instrument) -> Decimal:
    """Read ``instrument.min_notional`` as a Decimal, treating None as 0."""
    raw = getattr(instrument, "min_notional", None)
    if raw is None:
        return Decimal("0")
    # NT Money or Quantity-like type — convert via str for safety.
    if hasattr(raw, "as_decimal"):
        return raw.as_decimal()  # type: ignore[no-any-return]
    return Decimal(str(raw))


def _max_decimal(*values: Decimal | None) -> Decimal:
    """Return the max of the non-None values, or zero if all are None."""
    candidates = [v for v in values if v is not None]
    if not candidates:
        return Decimal("0")
    return max(candidates)
