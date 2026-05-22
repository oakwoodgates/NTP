"""Regression test for the docker-compose Postgres volume mount target.

Background
----------
The Postgres named volume `pgdata` MUST be mounted at the path the
running container uses as `PGDATA`. If the mount target doesn't match,
Postgres silently writes to its container writable layer instead of the
volume, and every container removal destroys the database.

This bug was latent in this repo from 2026-05-15 → 2026-05-22 (a stale
mount target at `/home/postgres/pgdata/data` left over from an older
timescale image convention). It cost six days of paper-trade history
when the security-hardening PR's full `docker compose down` finally
removed the Postgres container for the first time since initial setup.

The fix is a one-line correction in docker-compose.yml. This test pins
it so the bug can't silently regress (e.g., if someone copy-pastes the
old YAML or follows out-of-date timescale documentation).

Verifying the mount target after an image bump
----------------------------------------------
When bumping the timescale image version in docker-compose.yml:

1. Run the new image once: ``docker run --rm timescale/timescaledb:<new>
   env | grep PGDATA``
2. If PGDATA differs from `/var/lib/postgresql/data`, update both the
   compose mount target AND this test's expected value.
3. Stage A test pass criteria for any image bump: a fresh
   ``docker compose down`` + ``up -d postgres`` cycle preserves the
   data. The compose mount target is the load-bearing line.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

# The path inside the Postgres container where the database files live.
# For timescale/timescaledb:2.x-pg16 this is the standard Postgres
# convention (`/var/lib/postgresql/data`). See module docstring for what
# to do when bumping the image.
EXPECTED_PGDATA = "/var/lib/postgresql/data"


def _load_compose() -> dict[str, Any]:
    """Parse the docker-compose.yml at the repo root."""
    with COMPOSE_FILE.open(encoding="utf-8") as f:
        return cast("dict[str, Any]", yaml.safe_load(f))


def test_postgres_volume_mount_target_matches_pgdata() -> None:
    """The `pgdata` named volume must mount at the container's PGDATA path.

    Without this match, Postgres writes to the container's writable layer
    rather than the volume, and every container removal silently destroys
    the database.
    """
    compose = _load_compose()
    services = compose["services"]
    postgres = services["postgres"]
    volumes = postgres["volumes"]

    pgdata_mounts = [
        v for v in volumes if isinstance(v, str) and v.startswith("pgdata:")
    ]
    assert len(pgdata_mounts) == 1, (
        f"Expected exactly one `pgdata:...` mount on the postgres service; "
        f"got {pgdata_mounts}"
    )

    mount = pgdata_mounts[0]
    # Format: "pgdata:/container/path"
    target = mount.split(":", 1)[1]
    assert target == EXPECTED_PGDATA, (
        f"Postgres `pgdata` volume mount target is {target!r}, but the "
        f"timescale image's PGDATA is {EXPECTED_PGDATA!r}. With this "
        f"mismatch, Postgres writes to the container writable layer and "
        f"every `docker compose down` destroys the database. See the "
        f"module docstring for the verification recipe when bumping the "
        f"image version."
    )


def test_pgdata_volume_is_declared_at_top_level() -> None:
    """The `pgdata` volume must be declared in the top-level `volumes`
    section, otherwise compose treats it as an anonymous host bind.
    """
    compose = _load_compose()
    top_level_volumes = compose.get("volumes", {})
    assert isinstance(top_level_volumes, dict)
    assert "pgdata" in top_level_volumes, (
        "The `pgdata` named volume must be declared at the top-level "
        "`volumes:` section. Without the declaration, Docker creates an "
        "anonymous volume on first `up` and forgets it on `down`."
    )


@pytest.mark.parametrize("service", ["redis", "postgres"])
def test_infra_services_bind_loopback_only(service: str) -> None:
    """Postgres and Redis ports must be bound to ``127.0.0.1`` only,
    never ``0.0.0.0``. Docker's iptables manipulation bypasses UFW, so
    a bare ``"5434:5432"`` mapping exposes the port to the public
    internet even when UFW says only 22 and 3000 are allowed. See
    https://github.com/docker/for-linux/issues/690.
    """
    compose = _load_compose()
    services = compose["services"]
    svc = services[service]
    ports = svc.get("ports", [])
    assert ports, f"Service {service!r} has no ports declared"
    for entry in ports:
        # All port mappings on infra services must explicitly bind to 127.0.0.1.
        # We allow string mappings with the ``127.0.0.1:host_port:container_port`` form.
        assert isinstance(entry, str), (
            f"{service} port mapping must be a string with explicit bind: {entry!r}"
        )
        assert entry.startswith("127.0.0.1:"), (
            f"{service} port mapping {entry!r} is not bound to 127.0.0.1. "
            f"Docker bypasses UFW; binding to 0.0.0.0 exposes this service "
            f"to the public internet. Use the form "
            f"'127.0.0.1:<host_port>:<container_port>'."
        )
