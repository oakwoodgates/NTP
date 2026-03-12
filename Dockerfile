FROM python:3.12-slim

# Unbuffered stdout/stderr — mandatory for Docker logging.
# Without this, `docker compose logs -f` shows nothing for extended
# periods because Python buffers output until the 8KB buffer fills.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Non-root user — the process holds exchange API credentials in memory.
RUN useradd --create-home --shell /bin/bash trader

# System deps: libpq-dev for asyncpg's compiled extension.
# asyncpg ships pre-built wheels for linux/amd64 + CPython 3.12, so this
# is primarily a runtime dependency. If pip falls back to source build
# (non-amd64 arch, wheel yanked), build-essential would also be needed.
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy application code and install.
# All files are copied before pip install because hatchling (the build
# backend) expects src/ to exist — pyproject.toml declares
# packages = ["src"] under [tool.hatch.build.targets.wheel].
COPY pyproject.toml ./
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY alembic/ ./alembic/
COPY alembic.ini ./

RUN pip install --no-cache-dir .

# Entrypoint script — uses exec so Python becomes PID 1 and receives
# SIGTERM directly from `docker stop`.
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

USER trader

ENTRYPOINT ["./docker-entrypoint.sh"]
