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
