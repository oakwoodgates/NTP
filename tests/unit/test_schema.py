"""Tests for src.persistence.schema."""

import sqlalchemy as sa

from src.persistence.schema import (
    account_snapshots,
    metadata,
    order_fills,
    positions,
    strategy_runs,
)


class TestSchema:
    def test_four_tables_defined(self) -> None:
        assert len(metadata.tables) == 4

    def test_table_names(self) -> None:
        names = set(metadata.tables.keys())
        assert names == {"strategy_runs", "order_fills", "positions", "account_snapshots"}

    def test_strategy_runs_primary_key(self) -> None:
        pk_cols = [c.name for c in strategy_runs.primary_key.columns]
        assert pk_cols == ["id"]

    def test_order_fills_fk_to_strategy_runs(self) -> None:
        fks = [fk.target_fullname for c in order_fills.columns for fk in c.foreign_keys]
        assert "strategy_runs.id" in fks

    def test_positions_fk_to_strategy_runs(self) -> None:
        fks = [fk.target_fullname for c in positions.columns for fk in c.foreign_keys]
        assert "strategy_runs.id" in fks

    def test_account_snapshots_fk_to_strategy_runs(self) -> None:
        fks = [fk.target_fullname for c in account_snapshots.columns for fk in c.foreign_keys]
        assert "strategy_runs.id" in fks


class TestNumericColumns:
    """All financial values must be NUMERIC — never FLOAT."""

    NUMERIC_COLUMNS = [
        (order_fills, "last_qty"),
        (order_fills, "last_px"),
        (order_fills, "commission"),
        (positions, "peak_qty"),
        (positions, "avg_px_open"),
        (positions, "avg_px_close"),
        (positions, "realized_pnl"),
        (positions, "realized_return"),
        (account_snapshots, "balance_total"),
        (account_snapshots, "balance_free"),
        (account_snapshots, "balance_locked"),
    ]

    def test_all_financial_columns_are_numeric(self) -> None:
        for table, col_name in self.NUMERIC_COLUMNS:
            col = table.c[col_name]
            assert isinstance(col.type, sa.Numeric), (
                f"{table.name}.{col_name} should be NUMERIC, got {col.type}"
            )


class TestTimestampColumns:
    """All timestamp columns must be TIMESTAMP WITH TIMEZONE."""

    TIMESTAMP_COLUMNS = [
        (strategy_runs, "started_at"),
        (strategy_runs, "stopped_at"),
        (order_fills, "ts"),
        (positions, "ts_opened"),
        (positions, "ts_closed"),
        (account_snapshots, "ts"),
    ]

    def test_all_timestamp_columns_have_timezone(self) -> None:
        for table, col_name in self.TIMESTAMP_COLUMNS:
            col = table.c[col_name]
            assert isinstance(col.type, sa.TIMESTAMP), (
                f"{table.name}.{col_name} should be TIMESTAMP"
            )
            assert col.type.timezone is True, (
                f"{table.name}.{col_name} should have timezone=True"
            )
