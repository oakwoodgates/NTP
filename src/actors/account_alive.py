"""AccountAliveMonitor — emits ``AccountLiquidated`` and halts trading.

Subscribes to ``AccountState`` events on the message bus.  On each update,
runs the alive-predicate (``equity >= floor_im + fee_buffer`` for a
minimum-size entry).  When the predicate flips false:

1. Calls ``halt_callback`` — typically wired to
   ``engine.kernel.risk_engine.set_trading_state(TradingState.HALTED)``
   so subsequent ``submit_order`` calls are denied at the RiskEngine.
2. Publishes ``AccountLiquidated`` on the msgbus (latched — fires once).

Actors cannot submit orders; for position-liquidation order submission
see :class:`~src.core.liquidation_mixin.LiquidationAware`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig
from nautilus_trader.model.events import AccountState
from nautilus_trader.model.identifiers import Venue

from src.core.liquidation import (
    TOPIC_ACCOUNT_LIQUIDATED,
    AccountLiquidated,
    is_account_alive,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class AccountAliveMonitorConfig(ActorConfig, frozen=True):
    """Configuration for AccountAliveMonitor.

    All values here are **fully resolved** by ``make_engine`` before the
    actor is constructed (no Nones, no fallbacks at runtime).

    Attributes
    ----------
    venue : str
        The venue name (e.g. ``"HYPERLIQUID"``).  Used to look up the
        account from the cache.
    settlement_currency : str
        Settlement currency code (e.g. ``"USDC"``, ``"USDT"``).  Used to
        read equity from the account.
    venue_leverage : int
        Account leverage (e.g. 20 for 20x).  Used in the alive predicate.
    min_trade_notional : Decimal
        Minimum notional the alive predicate must be able to margin.
    fee_rate : Decimal
        Round-trip taker-fee rate buffer.
    alive_trades_buffer : int
        Require equity for N consecutive min-notional entries.

    """

    venue: str
    settlement_currency: str
    venue_leverage: int
    min_trade_notional: Decimal
    fee_rate: Decimal
    alive_trades_buffer: int = 1


class AccountAliveMonitor(Actor):
    """Watches account equity, halts trading when it can't margin a min entry.

    See module docstring for design rationale and constraints.
    """

    def __init__(
        self,
        config: AccountAliveMonitorConfig,
        halt_callback: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(config)
        # ``halt_callback`` is constructed at engine-build time and bound to
        # ``RiskEngine.set_trading_state(HALTED)``. The Actor itself stays
        # framework-clean — it doesn't import RiskEngine.
        self._halt_callback = halt_callback
        self._venue = Venue(config.venue)
        self._currency_code = config.settlement_currency
        self._latched = False

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def on_start(self) -> None:
        # NOTE: subscription to ``events.account.*`` is registered by
        # ``make_engine`` BEFORE ``engine.run()`` rather than here.
        # NT's MessageBus has a caching quirk where late wildcard
        # subscriptions don't fire for concrete topics whose cache is
        # already populated. Adding venue with starting balances triggers
        # an initial AccountState before this actor's ``on_start`` would
        # run, so a subscription registered here silently never fires.
        # See ``_register_account_alive_monitor`` in
        # ``src/backtesting/engine.py``.
        self.log.info(
            f"AccountAliveMonitor started "
            f"(min_trade_notional={self.config.min_trade_notional}, "
            f"floor_im≈{self._floor_im()}, fee_buffer≈{self._fee_buffer()})",
        )

    def on_reset(self) -> None:
        # Clear latch so the same engine can be reused across sweep iterations.
        self._latched = False

    # ── Predicate ───────────────────────────────────────────────────────────

    def _floor_im(self) -> Decimal:
        return self.config.min_trade_notional / Decimal(self.config.venue_leverage)  # type: ignore[no-any-return]

    def _fee_buffer(self) -> Decimal:
        return (  # type: ignore[no-any-return]
            self.config.min_trade_notional
            * self.config.fee_rate
            * 2
            * self.config.alive_trades_buffer
        )

    def _required(self) -> Decimal:
        return self._floor_im() + self._fee_buffer()

    # ── Event handler ───────────────────────────────────────────────────────

    def _on_account_state(self, event: Any) -> None:
        # Defensive: msgbus wildcard delivers anything routed there.
        if not isinstance(event, AccountState):
            return
        if self._latched:
            return

        equity = self._extract_equity(event)
        if equity is None:
            return  # Couldn't resolve a balance in our settlement currency

        alive = is_account_alive(
            equity=equity,
            min_trade_notional=self.config.min_trade_notional,
            venue_leverage=self.config.venue_leverage,
            fee_rate=self.config.fee_rate,
            alive_trades_buffer=self.config.alive_trades_buffer,
        )
        if alive:
            return

        # Latch first so a halt-callback failure or a re-entrant publish
        # doesn't fire the path twice.
        self._latched = True

        required = self._required()
        liq_event = AccountLiquidated(
            equity=equity,
            required=required,
            ts_event=event.ts_event,
        )

        self.log.warning(
            f"ACCOUNT LIQUIDATED: equity={equity} below required={required} "
            f"(min_trade_notional={self.config.min_trade_notional}, "
            f"leverage={self.config.venue_leverage}x, fee_rate={self.config.fee_rate})",
        )

        # Halt first, then publish — denying new orders takes precedence
        # over notifying subscribers.
        if self._halt_callback is not None:
            try:
                self._halt_callback()
            except Exception as e:  # pragma: no cover - defensive
                self.log.error(f"halt_callback raised: {e}")

        self.msgbus.publish(topic=TOPIC_ACCOUNT_LIQUIDATED, msg=liq_event)

    def _extract_equity(self, event: AccountState) -> Decimal | None:
        """Pull total equity from the account state's balances.

        AccountState carries a list of ``AccountBalance`` objects, one per
        currency. We pick the one matching our configured settlement
        currency and read its ``total``.
        """
        for balance in event.balances:
            currency = getattr(balance, "currency", None)
            currency_code = getattr(currency, "code", None) or str(currency)
            if currency_code == self._currency_code:
                total = balance.total
                if hasattr(total, "as_decimal"):
                    return total.as_decimal()  # type: ignore[no-any-return]
                return Decimal(str(total))
        return None
