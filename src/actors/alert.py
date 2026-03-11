"""AlertActor — sends Telegram notifications on fills, position closes, and drawdown."""

from decimal import Decimal

import httpx
import pandas as pd
from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderFilled, PositionClosed
from nautilus_trader.model.identifiers import Venue


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
        side_emoji = "+" if event.order_side == OrderSide.BUY else "-"
        msg = (
            f"<b>Fill</b>: {side_emoji}{event.order_side.name}\n"
            f"Instrument: {event.instrument_id}\n"
            f"Qty: {event.last_qty}  @  {event.last_px}"
        )
        self.run_in_executor(self._send, (msg,))

    def on_event(self, event) -> None:  # noqa: ANN001
        if isinstance(event, PositionClosed) and self.config.notify_position_closed:
            pnl = event.realized_pnl
            pnl_str = str(pnl) if pnl else "N/A"
            try:
                pnl_val = float(str(pnl).split()[0]) if pnl else 0.0
                result = "WIN" if pnl_val >= 0 else "LOSS"
            except (ValueError, AttributeError):
                result = "CLOSED"
            msg = (
                f"<b>Position {result}</b>: {event.instrument_id}\n"
                f"PnL: {pnl_str}\n"
                f"Return: {event.realized_return:.4f}"
            )
            self.run_in_executor(self._send, (msg,))

    def _on_drawdown_check(self, event) -> None:  # noqa: ANN001
        self.run_in_executor(self._check_drawdown)

    def _check_drawdown(self) -> None:
        account = self.cache.account_for_venue(self._venue)
        if account is None:
            return
        balance = account.balance(USDC)
        if balance is None:
            return
        current = Decimal(str(balance.total))
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
