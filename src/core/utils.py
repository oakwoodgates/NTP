"""Pure utility functions — no business logic, no internal imports beyond core/."""

from src.core.constants import INTERVAL_TO_BAR_SPEC


def bar_type_str(instrument_id: str, interval: str) -> str:
    """Build an EXTERNAL BarType string from instrument ID and candle interval."""
    step, agg = INTERVAL_TO_BAR_SPEC[interval]
    return f"{instrument_id}-{step}-{agg}-LAST-EXTERNAL"
