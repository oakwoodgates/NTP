"""strategy_runs.parent_run_id — link consecutive runs of the same trader.

Phase 2.5 / Stage B prerequisite. Each restart still inserts a fresh row
(no "resume the previous run" semantics — see PR description for the
trade-off versus approach (A)). The new ``parent_run_id`` column points
at the most-recent prior row for the same
``(trader_id, instrument_id, strategy_id, run_mode)`` tuple, so
cross-restart queries can walk back through the chain with a recursive
CTE instead of UNIONing UUIDs.

Index ``ix_strategy_runs_lookup`` supports the lookup the runners do in
``_open_run`` — equality on the four key columns, sort by
``started_at DESC``.

Revision ID: 005
Revises: 004
Create Date: 2026-05-20
"""

import sqlalchemy as sa

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategy_runs",
        sa.Column(
            "parent_run_id",
            sa.UUID,
            sa.ForeignKey("strategy_runs.id"),
            nullable=True,
        ),
    )
    # Lookup index for `_open_run`: most-recent prior run for this trader
    # × instrument × strategy × mode. Equality on the four columns, sort
    # on started_at DESC. strategy_runs grows by ~one row per deploy,
    # so this stays cheap.
    op.create_index(
        "ix_strategy_runs_lookup",
        "strategy_runs",
        ["trader_id", "instrument_id", "strategy_id", "run_mode", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_strategy_runs_lookup")
    op.drop_column("strategy_runs", "parent_run_id")
