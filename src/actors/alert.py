"""AlertActor — sends Telegram notifications on fills, position closes, and drawdown."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pandas as pd
from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderFilled, PositionClosed
from nautilus_trader.model.identifiers import Venue


def _valid_instrument_id(s: str) -> bool:
    """Expect instrument ids like 'BTC-USDT.HYPERLIQUID' (symbol-quote.venue)."""
    s = str(s).strip()
    return len(s) >= 2 and "." in s and "-" in s


def _valid_decimal_str(s: str) -> bool:
    """Check that a string parses as a non-negative decimal (for qty/price)."""
    try:
        d = Decimal(str(s).strip())
        return d >= 0
    except Exception:
        return False


class AlertActorConfig(ActorConfig, frozen=True):
    telegram_token: str = ""
    telegram_chat_id: str = ""
    enabled: bool = False
    venue: str = "HYPERLIQUID"
    notify_fills: bool = True
    notify_position_closed: bool = True
    drawdown_alert_pct: str = "10"  # str to avoid float — Decimal in actor
    drawdown_check_interval_secs: int = 300


class AlertActor(Actor):

    def __init__(self, config: AlertActorConfig) -> None:
        super().__init__(config)
        self._drawdown_threshold = Decimal(config.drawdown_alert_pct) / 100
        self._peak_balance: Decimal | None = None
        self._alerted_drawdown = False
        self._venue = Venue(config.venue)

    def on_start(self) -> None:
        if self.config.drawdown_check_interval_secs > 0:
            self.clock.set_timer(
                name="drawdown_check",
                interval=pd.Timedelta(seconds=self.config.drawdown_check_interval_secs),
                callback=self._on_drawdown_check,
            )
        self.run_in_executor(self._send, ("TradingNode started",))

    def on_order_filled(self, event: OrderFilled) -> None:
        if not self.config.notify_fills:
            return
        instrument_id_str = str(event.instrument_id)
        last_qty_str = str(event.last_qty)
        last_px_str = str(event.last_px)
        if not _valid_instrument_id(instrument_id_str):
            self.log.warning(
                "AlertActor: skipping fill alert — unexpected instrument_id shape (OrderFilled)",
                extra={
                    "event": "OrderFilled",
                    "instrument_id": instrument_id_str,
                    "strategy_id": str(event.strategy_id),
                },
            )
            return
        if not _valid_decimal_str(last_qty_str) or not _valid_decimal_str(last_px_str):
            self.log.warning(
                "AlertActor: skipping fill alert — invalid qty/price",
                extra={
                    "event": "OrderFilled",
                    "instrument_id": instrument_id_str,
                    "last_qty": last_qty_str,
                    "last_px": last_px_str,
                },
            )
            return
        side_emoji = "+" if event.order_side == OrderSide.BUY else "-"
        msg = (
            f"<b>Fill</b>: {side_emoji}{event.order_side.name}\n"
            f"Instrument: {event.instrument_id}\n"
            f"Qty: {event.last_qty}  @  {event.last_px}"
        )
        self.run_in_executor(self._send, (msg,))

    def on_event(self, event: Any) -> None:
        if isinstance(event, PositionClosed) and self.config.notify_position_closed:
            instrument_id_str = str(event.instrument_id)
            if not _valid_instrument_id(instrument_id_str):
                self.log.warning(
                    "AlertActor: skipping position alert — unexpected instrument_id shape (PositionClosed)",
                    extra={
                        "event": "PositionClosed",
                        "instrument_id": instrument_id_str,
                        "strategy_id": str(event.strategy_id),
                    },
                )
                return
            pnl = event.realized_pnl
            pnl_str = str(pnl) if pnl else "N/A"
            try:
                pnl_val = pnl.as_decimal() if pnl else Decimal("0")
                result = "WIN" if pnl_val >= Decimal("0") else "LOSS"
            except (ValueError, AttributeError):
                result = "CLOSED"
            msg = (
                f"<b>Position {result}</b>: {event.instrument_id}\n"
                f"PnL: {pnl_str}\n"
                f"Return: {event.realized_return:.4f}"
            )
            self.run_in_executor(self._send, (msg,))

    def _on_drawdown_check(self, event: Any) -> None:
        self.run_in_executor(self._check_drawdown)

    def _check_drawdown(self) -> None:
        account = self.cache.account_for_venue(self._venue)
        if account is None:
            return
        balance = account.balance(USDC)
        if balance is None:
            return
        current = balance.total.as_decimal()
        if self._peak_balance is None or current > self._peak_balance:
            self._peak_balance = current
            self._alerted_drawdown = False
            return
        if self._peak_balance == 0:
            return
        drawdown = (self._peak_balance - current) / self._peak_balance
        if drawdown >= self._drawdown_threshold and not self._alerted_drawdown:
            self._alerted_drawdown = True
            self._send(
                f"<b>DRAWDOWN ALERT</b>: {drawdown * 100:.1f}%\n"
                f"Peak: {self._peak_balance} USDC\n"
                f"Current: {current} USDC"
            )

    def on_stop(self) -> None:
        self.run_in_executor(self._send, ("TradingNode stopped",))

    def _send(self, text: str) -> None:
        if not self.config.enabled or not self.config.telegram_token:
            self.log.debug(f"AlertActor (disabled): {text}")
            return
        url = f"https://api.telegram.org/bot{self.config.telegram_token}/sendMessage"
        try:
            httpx.post(
                url,
                json={
                    "chat_id": self.config.telegram_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=5.0,
            )
        except Exception as e:
            self.log.warning(f"AlertActor: Telegram send failed: {e}")
