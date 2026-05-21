"""SQLAlchemy Core table definitions for Phase 2 persistence.

Used by Alembic for migrations. PersistenceActor uses raw asyncpg SQL strings
(not these table objects) to avoid coupling actors/ to persistence/.

All financial values are NUMERIC — never FLOAT, DOUBLE PRECISION, or REAL.
All timestamps are TIMESTAMPTZ (with timezone).
Every table includes strategy_id and run_id for multi-strategy, multi-run support.
"""

import sqlalchemy as sa

metadata = sa.MetaData()

strategy_runs = sa.Table(
    "strategy_runs",
    metadata,
    sa.Column("id", sa.UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("trader_id", sa.Text, nullable=False),
    sa.Column("strategy_id", sa.Text, nullable=False),
    sa.Column("instrument_id", sa.Text, nullable=False),
    sa.Column("run_mode", sa.Text, nullable=False),  # 'sandbox' | 'live'
    sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("stopped_at", sa.TIMESTAMP(timezone=True)),
    sa.Column("config", sa.JSON, nullable=False),
    # Self-FK to the most-recent prior run of the same
    # (trader_id, instrument_id, strategy_id, run_mode) tuple. Each
    # process restart inserts a fresh row; this column lets cross-restart
    # queries walk the chain via a recursive CTE rather than UNIONing
    # individual UUIDs together. NULL on the first run of a tuple.
    sa.Column("parent_run_id", sa.UUID, sa.ForeignKey("strategy_runs.id")),
)

order_fills = sa.Table(
    "order_fills",
    metadata,
    sa.Column("ts", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("run_id", sa.UUID, sa.ForeignKey("strategy_runs.id"), nullable=False),
    sa.Column("strategy_id", sa.Text, nullable=False),
    sa.Column("instrument_id", sa.Text, nullable=False),
    sa.Column("client_order_id", sa.Text, nullable=False),
    sa.Column("venue_order_id", sa.Text),
    sa.Column("trade_id", sa.Text),
    sa.Column("order_side", sa.Text, nullable=False),  # 'BUY' | 'SELL'
    sa.Column("last_qty", sa.Numeric, nullable=False),
    sa.Column("last_px", sa.Numeric, nullable=False),
    sa.Column("commission", sa.Numeric),
    sa.Column("commission_currency", sa.Text),
    sa.Column("liquidity_side", sa.Text),  # 'MAKER' | 'TAKER' | 'NO_LIQUIDITY_SIDE'
)

positions = sa.Table(
    "positions",
    metadata,
    sa.Column("ts_opened", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("ts_closed", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("run_id", sa.UUID, sa.ForeignKey("strategy_runs.id"), nullable=False),
    sa.Column("strategy_id", sa.Text, nullable=False),
    sa.Column("instrument_id", sa.Text, nullable=False),
    sa.Column("position_id", sa.Text, nullable=False),
    sa.Column("entry_side", sa.Text, nullable=False),  # 'BUY' | 'SELL'
    sa.Column("peak_qty", sa.Numeric, nullable=False),
    sa.Column("avg_px_open", sa.Numeric),
    sa.Column("avg_px_close", sa.Numeric),
    sa.Column("realized_pnl", sa.Numeric),
    sa.Column("realized_return", sa.Numeric),  # fraction — 0.05 = 5%
    sa.Column("currency", sa.Text),
    sa.Column("duration_ns", sa.BigInteger),
    # closing_order_id: the order that flattened the position. Forensic
    # link from positions back to order_fills.client_order_id.
    sa.Column("closing_order_id", sa.Text),
    # close_reason: one of
    #   'protective_stop' — the ProtectiveStopAware mixin's reduce-only stop fired
    #   'liquidation'     — the LiquidationAware mixin's reduce-only stop fired
    #   'strategy_exit'   — the strategy's cross-flip exit market order
    #   'unknown'         — closing order had no recognised tag, or order not in cache
    # Populated by PersistenceActor by inspecting the closing order's tags
    # at PositionClosed event time. NULL for rows written before migration 003.
    sa.Column("close_reason", sa.Text),
)

account_snapshots = sa.Table(
    "account_snapshots",
    metadata,
    sa.Column("ts", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("run_id", sa.UUID, sa.ForeignKey("strategy_runs.id"), nullable=False),
    sa.Column("venue", sa.Text, nullable=False),
    sa.Column("currency", sa.Text, nullable=False),
    sa.Column("balance_total", sa.Numeric, nullable=False),
    sa.Column("balance_free", sa.Numeric, nullable=False),
    sa.Column("balance_locked", sa.Numeric, nullable=False),
)

# Per-bar signal-gate output. One row per bar after indicator warmup,
# emitted by signal-generating strategies (e.g., MACross). ``acted=true``
# rows are the bars the strategy actually entered/flipped on; the rest
# record the gate state so we can reconstruct the full signal stream and
# align it against backtest cross times (Phase 2.5 verification).
signal_events = sa.Table(
    "signal_events",
    metadata,
    sa.Column("ts", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("run_id", sa.UUID, sa.ForeignKey("strategy_runs.id"), nullable=False),
    sa.Column("strategy_id", sa.Text, nullable=False),
    sa.Column("instrument_id", sa.Text, nullable=False),
    sa.Column("signal", sa.SmallInteger, nullable=False),  # +1 LONG, -1 SHORT, 0 NONE
    sa.Column("fast_value", sa.Numeric, nullable=False),
    sa.Column("slow_value", sa.Numeric, nullable=False),
    sa.Column("acted", sa.Boolean, nullable=False),
    sa.Column("bootstrap", sa.Boolean, nullable=False),
    # bar_close: the closing price of the bar at the moment SignalEvent
    # fired. Used by dashboard for mark-to-market unrealized PnL on the
    # open position. NULL for rows written before migration 004.
    sa.Column("bar_close", sa.Numeric),
)
