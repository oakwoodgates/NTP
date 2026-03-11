"""PersistenceActor — writes fills, positions, and account snapshots to PostgreSQL.

Subscribes to NT MessageBus events inside the TradingNode and persists them
via asyncpg. All I/O runs in the executor to avoid blocking the event loop.
"""

import asyncio
import uuid
from datetime import UTC, datetime

import asyncpg
import pandas as pd
from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.events import OrderFilled, PositionClosed
from nautilus_trader.model.identifiers import Venue


class PersistenceActorConfig(ActorConfig, frozen=True):
    postgres_dsn: str
    run_id: str  # UUID string — ties rows to this TradingNode run
    venue: str = "HYPERLIQUID"
    snapshot_interval_secs: int = 60


class PersistenceActor(Actor):

    def __init__(self, config: PersistenceActorConfig) -> None:
        super().__init__(config)
        self._venue = Venue(config.venue)

    def on_start(self) -> None:
        self.clock.set_timer(
            name="account_snapshot",
            interval=pd.Timedelta(seconds=self.config.snapshot_interval_secs),
            callback=self._on_snapshot_timer,
        )
        self.log.info("PersistenceActor started")

    def on_order_filled(self, event: OrderFilled) -> None:
        self.run_in_executor(self._persist_fill, (event,))

    def on_event(self, event) -> None:  # noqa: ANN001
        if isinstance(event, PositionClosed):
            self.run_in_executor(self._persist_position, (event,))

    def _on_snapshot_timer(self, event) -> None:  # noqa: ANN001
        self.run_in_executor(self._persist_account_snapshot)

    # -- Executor callables (run in ThreadPoolExecutor) -----------------------

    def _persist_fill(self, event: OrderFilled) -> None:
        ts = datetime.fromtimestamp(event.ts_event / 1e9, tz=UTC)
        commission = event.commission
        try:
            asyncio.run(self._async_insert_fill(
                ts,
                uuid.UUID(self.config.run_id),
                str(event.strategy_id),
                str(event.instrument_id),
                str(event.client_order_id),
                str(event.venue_order_id) if event.venue_order_id else None,
                str(event.trade_id) if event.trade_id else None,
                event.order_side.name,
                str(event.last_qty),
                str(event.last_px),
                str(commission.as_decimal()) if commission else None,
                str(commission.currency) if commission else None,
                event.liquidity_side.name,
            ))
        except Exception as e:
            self.log.error(f"PersistenceActor: fill insert failed: {e}")

    def _persist_position(self, event: PositionClosed) -> None:
        ts_opened = datetime.fromtimestamp(event.ts_opened / 1e9, tz=UTC)
        ts_closed = datetime.fromtimestamp(event.ts_closed / 1e9, tz=UTC)
        # avg_px_open/avg_px_close are doubles — convert via str to preserve
        # precision for NUMERIC column (avoid float→Decimal precision artifacts)
        try:
            asyncio.run(self._async_insert_position(
                ts_opened,
                ts_closed,
                uuid.UUID(self.config.run_id),
                str(event.strategy_id),
                str(event.instrument_id),
                str(event.position_id),
                event.entry.name,
                str(event.peak_qty),
                str(event.avg_px_open),
                str(event.avg_px_close),
                str(event.realized_pnl.as_decimal()) if event.realized_pnl else None,
                str(event.realized_return),
                str(event.currency),
                event.duration_ns,
            ))
        except Exception as e:
            self.log.error(f"PersistenceActor: position insert failed: {e}")

    def _persist_account_snapshot(self) -> None:
        account = self.cache.account_for_venue(self._venue)
        if account is None:
            return
        balance = account.balance(USDC)
        if balance is None:
            return
        ts = datetime.now(tz=UTC)
        balance_total = str(balance.total.as_decimal())
        balance_free = str(balance.free.as_decimal())
        balance_locked = str(balance.locked.as_decimal())
        try:
            asyncio.run(self._async_insert_snapshot(
                ts,
                uuid.UUID(self.config.run_id),
                self.config.venue,
                "USDC",
                balance_total,
                balance_free,
                balance_locked,
            ))
        except Exception as e:
            self.log.error(f"PersistenceActor: snapshot insert failed: {e}")

    # -- Async DB operations --------------------------------------------------

    async def _async_insert_fill(
        self,
        ts: datetime,
        run_id: uuid.UUID,
        strategy_id: str,
        instrument_id: str,
        client_order_id: str,
        venue_order_id: str | None,
        trade_id: str | None,
        order_side: str,
        last_qty: str,
        last_px: str,
        commission: str | None,
        commission_currency: str | None,
        liquidity_side: str,
    ) -> None:
        conn = await asyncpg.connect(self.config.postgres_dsn)
        try:
            await conn.execute(
                """
                INSERT INTO order_fills (
                    ts, run_id, strategy_id, instrument_id, client_order_id,
                    venue_order_id, trade_id, order_side, last_qty, last_px,
                    commission, commission_currency, liquidity_side
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                """,
                ts, run_id, strategy_id, instrument_id, client_order_id,
                venue_order_id, trade_id, order_side, last_qty, last_px,
                commission, commission_currency, liquidity_side,
            )
        finally:
            await conn.close()

    async def _async_insert_position(
        self,
        ts_opened: datetime,
        ts_closed: datetime,
        run_id: uuid.UUID,
        strategy_id: str,
        instrument_id: str,
        position_id: str,
        entry_side: str,
        peak_qty: str,
        avg_px_open: str,
        avg_px_close: str,
        realized_pnl: str | None,
        realized_return: str,
        currency: str,
        duration_ns: int,
    ) -> None:
        conn = await asyncpg.connect(self.config.postgres_dsn)
        try:
            await conn.execute(
                """
                INSERT INTO positions (
                    ts_opened, ts_closed, run_id, strategy_id, instrument_id,
                    position_id, entry_side, peak_qty, avg_px_open, avg_px_close,
                    realized_pnl, realized_return, currency, duration_ns
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                """,
                ts_opened, ts_closed, run_id, strategy_id, instrument_id,
                position_id, entry_side, peak_qty, avg_px_open, avg_px_close,
                realized_pnl, realized_return, currency, duration_ns,
            )
        finally:
            await conn.close()

    async def _async_insert_snapshot(
        self,
        ts: datetime,
        run_id: uuid.UUID,
        venue: str,
        currency: str,
        balance_total: str,
        balance_free: str,
        balance_locked: str,
    ) -> None:
        conn = await asyncpg.connect(self.config.postgres_dsn)
        try:
            await conn.execute(
                """
                INSERT INTO account_snapshots (
                    ts, run_id, venue, currency,
                    balance_total, balance_free, balance_locked
                ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                """,
                ts, run_id, venue, currency,
                balance_total, balance_free, balance_locked,
            )
        finally:
            await conn.close()

    def on_stop(self) -> None:
        self.log.info("PersistenceActor stopped")
