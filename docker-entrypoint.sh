#!/bin/sh
set -e

# If arguments were passed (e.g., `docker compose run --rm trader alembic upgrade head`),
# run them directly. This enables ad-hoc commands for migrations, verification, and debugging.
if [ $# -gt 0 ]; then
    exec "$@"
fi

# Default: run the trading script.
# `exec` replaces the shell with Python, making Python PID 1.
# This is critical: `docker stop` sends SIGTERM to PID 1. Without exec,
# sh is PID 1, receives SIGTERM, exits, and Python gets SIGKILL —
# bypassing all graceful shutdown logic in the runner scripts.
#
# Signal flow: docker stop → SIGTERM → PID 1 (Python) → signal handler
# → raises KeyboardInterrupt → except/finally in main() → node.stop()
# → _close_run() updates strategy_runs.stopped_at → process exits.
exec python "${TRADING_SCRIPT:-scripts/run_sandbox.py}"
