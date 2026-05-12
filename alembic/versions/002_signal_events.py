"""signal_events — per-bar signal gate output for paper-vs-backtest alignment.

Revision ID: 002
Revises: 001
Create Date: 2026-05-11
"""

import sqlalchemy as sa

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signal_events",
        sa.Column("ts", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("run_id", sa.UUID, sa.ForeignKey("strategy_runs.id"), nullable=False),
        sa.Column("strategy_id", sa.Text, nullable=False),
        sa.Column("instrument_id", sa.Text, nullable=False),
        sa.Column("signal", sa.SmallInteger, nullable=False),
        sa.Column("fast_value", sa.Numeric, nullable=False),
        sa.Column("slow_value", sa.Numeric, nullable=False),
        sa.Column("acted", sa.Boolean, nullable=False),
        sa.Column("bootstrap", sa.Boolean, nullable=False),
    )

    op.execute("SELECT create_hypertable('signal_events', 'ts')")
    op.create_index(
        "ix_signal_events_run_strategy_ts",
        "signal_events",
        ["run_id", "strategy_id", "ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_signal_events_run_strategy_ts")
    op.drop_table("signal_events")
