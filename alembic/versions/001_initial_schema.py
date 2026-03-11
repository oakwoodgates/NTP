"""Initial schema — strategy_runs, order_fills, positions, account_snapshots.

Revision ID: 001
Revises:
Create Date: 2026-03-10
"""

import sqlalchemy as sa

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable TimescaleDB extension
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    # strategy_runs — one row per TradingNode run (NOT a hypertable)
    op.create_table(
        "strategy_runs",
        sa.Column("id", sa.UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("trader_id", sa.Text, nullable=False),
        sa.Column("strategy_id", sa.Text, nullable=False),
        sa.Column("instrument_id", sa.Text, nullable=False),
        sa.Column("run_mode", sa.Text, nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("config", sa.JSON, nullable=False),
    )

    # order_fills — one row per OrderFilled event
    op.create_table(
        "order_fills",
        sa.Column("ts", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("run_id", sa.UUID, sa.ForeignKey("strategy_runs.id"), nullable=False),
        sa.Column("strategy_id", sa.Text, nullable=False),
        sa.Column("instrument_id", sa.Text, nullable=False),
        sa.Column("client_order_id", sa.Text, nullable=False),
        sa.Column("venue_order_id", sa.Text),
        sa.Column("trade_id", sa.Text),
        sa.Column("order_side", sa.Text, nullable=False),
        sa.Column("last_qty", sa.Numeric, nullable=False),
        sa.Column("last_px", sa.Numeric, nullable=False),
        sa.Column("commission", sa.Numeric),
        sa.Column("commission_currency", sa.Text),
        sa.Column("liquidity_side", sa.Text),
    )

    # positions — one row per PositionClosed event
    op.create_table(
        "positions",
        sa.Column("ts_opened", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ts_closed", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("run_id", sa.UUID, sa.ForeignKey("strategy_runs.id"), nullable=False),
        sa.Column("strategy_id", sa.Text, nullable=False),
        sa.Column("instrument_id", sa.Text, nullable=False),
        sa.Column("position_id", sa.Text, nullable=False),
        sa.Column("entry_side", sa.Text, nullable=False),
        sa.Column("peak_qty", sa.Numeric, nullable=False),
        sa.Column("avg_px_open", sa.Numeric),
        sa.Column("avg_px_close", sa.Numeric),
        sa.Column("realized_pnl", sa.Numeric),
        sa.Column("realized_return", sa.Numeric),
        sa.Column("currency", sa.Text),
        sa.Column("duration_ns", sa.BigInteger),
    )

    # account_snapshots — periodic balance snapshots
    op.create_table(
        "account_snapshots",
        sa.Column("ts", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("run_id", sa.UUID, sa.ForeignKey("strategy_runs.id"), nullable=False),
        sa.Column("venue", sa.Text, nullable=False),
        sa.Column("currency", sa.Text, nullable=False),
        sa.Column("balance_total", sa.Numeric, nullable=False),
        sa.Column("balance_free", sa.Numeric, nullable=False),
        sa.Column("balance_locked", sa.Numeric, nullable=False),
    )

    # Convert time-series tables to TimescaleDB hypertables
    op.execute("SELECT create_hypertable('order_fills', 'ts')")
    op.execute("SELECT create_hypertable('positions', 'ts_closed')")
    op.execute("SELECT create_hypertable('account_snapshots', 'ts')")

    # Indexes for common query patterns
    op.create_index("ix_order_fills_run_strategy", "order_fills", ["run_id", "strategy_id"])
    op.create_index("ix_positions_run_strategy", "positions", ["run_id", "strategy_id"])


def downgrade() -> None:
    op.drop_index("ix_positions_run_strategy")
    op.drop_index("ix_order_fills_run_strategy")
    op.drop_table("account_snapshots")
    op.drop_table("positions")
    op.drop_table("order_fills")
    op.drop_table("strategy_runs")
