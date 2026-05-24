# Deploy to Digital Ocean

## Droplet Spec

- **Image:** Docker 1-Click from DO Marketplace (Ubuntu + Docker + Compose pre-installed)
- **Size:** 2GB RAM / 1 vCPU ($12/mo) for single-instrument deploys. **4GB ($24/mo) for multi-instrument** (three trader containers + infra). 1GB is too tight.
- **Region:** Close to you for SSH latency. Trading latency is irrelevant — minute-or-longer bars, not HFT.

## Trader topology — pick one

All trader services are **profile-gated**. Plain `docker compose up -d`
starts only infra (postgres + redis + grafana); you must specify a
profile to start a trader.

| Profile | Services started | When to use |
|---|---|---|
| `single` | `trader` (reads strategy + instrument from `.env`) | Legacy single-instrument paper trading |
| `eth` / `btc` / `sol` | `trader-eth` / `trader-btc` / `trader-sol` — each reads strategy from `.env.{asset}` | Phase 2.5/2.6 verification, one instrument at a time |
| `multi` | All three per-instrument traders | Full multi-instrument verification deploy |

The rest of this doc shows both flows. Pick whichever fits your goal
and substitute the profile flag accordingly.

## One-Time Setup

```bash
ssh root@<droplet-ip>

# 1. Firewall
ufw allow 22/tcp
ufw allow 3000/tcp    # Grafana — or skip and use SSH tunnel (see below)
ufw enable

# 2. Clone repo
git clone <your-repo-url> ~/NTP
cd ~/NTP

# 3. Create base .env
cp .env.example .env
nano .env
# REQUIRED — set strong values for the secrets:
#   POSTGRES_PASSWORD
#   REDIS_PASSWORD   (Redis container is launched with --requirepass; trader
#                     can't connect without this)
#   GRAFANA_PASSWORD
# Generate any of these with: openssl rand -base64 32
#
# Recommended: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID for alerts
# Default behaviour: TRADING_SCRIPT=scripts/run_sandbox.py (paper trading via sandbox exec)
# Sandbox runs against MAINNET HL data feed with simulated fills — you do NOT
# need HL_PRIVATE_KEY or HL_WALLET_ADDRESS for sandbox. HL_TESTNET=true is only
# honoured by scripts/run_live.py (real testnet exchange).
#
# Risk-management knobs (defaults shown):
#   STOP_PCT=0.05               protective-stop fraction (renamed from DEFAULT_STOP_PCT;
#                               operators migrating from older deploys must rename their var)
#   BOOTSTRAP_ON_DEPLOY=false   set `true` for live mid-trend deploys; leave `false` for verification
#   STARTING_CAPITAL=1000, TRADE_NOTIONAL=2000, LEVERAGE=20

# 4. (Multi-instrument only — skip for single-instrument) Per-instrument config files
cp .env.eth.example .env.eth        # then edit with your ETH picks
cp .env.btc.example .env.btc        # then edit with your BTC picks
cp .env.sol.example .env.sol        # then edit with your SOL picks
# Each per-instrument file must contain at minimum: STRATEGY, INSTRUMENT_ID,
# BAR_INTERVAL, STOP_PCT, plus strategy-specific params (MA_FAST/MA_SLOW/MA_TYPE
# for MACross). These files are gitignored — values stay on the host.

# 5. Build the trader image (takes a few minutes first time — NT is ~1-2GB)
docker compose build trader
# This produces a single `ntp-trader:latest` image shared by every
# profile-gated trader service (trader / trader-eth / trader-btc /
# trader-sol). One build is all you need.

# 6. Start infrastructure
docker compose up -d
# This starts ONLY postgres + redis + grafana (no trader, by design).
docker compose ps    # postgres should show "healthy" before proceeding

# 7. Run migrations FIRST — before any trader starts
docker compose --profile single run --rm trader alembic upgrade head
# (--profile single is needed so compose can locate the trader service for the one-shot run)

# 8a. Single-instrument flow — start the legacy trader
docker compose --profile single up -d trader
docker compose logs -f trader --tail 50

# 8b. OR multi-instrument flow — start per-instrument traders
docker compose --profile eth up -d trader-eth
# Add btc/sol similarly, or `docker compose --profile multi up -d` for all three
docker compose logs -f trader-eth --tail 50

# 9. Verify services
docker compose ps
# Single: postgres (healthy), redis (healthy), grafana, trader
# Multi: postgres + redis + grafana + trader-eth + trader-btc + trader-sol
```

**Migration ordering matters.** Each trader calls `_register_run()` on startup, which
INSERTs into `strategy_runs`. If migrations haven't run, the table doesn't exist, the
trader crashes and enters a restart loop. Always run alembic before bringing up traders.

**`.env.{asset}` is required for per-instrument profiles.** If you start `--profile eth`
without creating `.env.eth`, compose fails at `up` time with "env file not found." That's
intentional — better than silently using `.env` defaults and trading the wrong instrument.

## Verify It's Working

```bash
# After a few minutes, check infra-level DB writes (one row per 60s)
docker compose exec postgres psql -U nautilus -d nautilus_platform \
    -c "SELECT COUNT(*) FROM account_snapshots;"

# Check strategy run was registered (substitute trader-eth/btc/sol for multi)
docker compose exec postgres psql -U nautilus -d nautilus_platform \
    -c "SELECT id, trader_id, strategy_id, started_at, stopped_at FROM strategy_runs ORDER BY started_at DESC LIMIT 3;"

# Per-bar gate stream flowing (one row per bar after MA warmup; key Phase 2.5 telemetry)
docker compose exec postgres psql -U nautilus -d nautilus_platform \
    -c "SELECT COUNT(*), MIN(ts), MAX(ts) FROM signal_events;"

# AccountAliveMonitor subscribed (substitute trader-eth etc. for multi-instrument)
docker compose logs trader | grep "AccountAliveMonitor started"

# Per-bar log line from MACross
docker compose logs trader | grep "cross_gate:" | tail -5

# Sanity: no blocked-callback warnings
docker compose logs trader | grep -i "blocked"

# Test graceful shutdown
docker compose stop trader
# Check stopped_at is NOT NULL:
docker compose exec postgres psql -U nautilus -d nautilus_platform \
    -c "SELECT stopped_at FROM strategy_runs ORDER BY started_at DESC LIMIT 1;"
# stopped_at MUST be NOT NULL — if it is NULL, the SIGTERM chain is broken

# Restart (compose remembers profile association; no flag needed for start/restart)
docker compose start trader
```

## Monitoring

### Grafana over public port (simpler)

```
http://<droplet-ip>:3000
# Login: admin / your GRAFANA_PASSWORD from .env
```

### SSH tunnel (more secure — remove the UFW 3000 rule)

```bash
# From your local machine:
ssh -L 3000:localhost:3000 root@<droplet-ip>
# Then open http://localhost:3000 locally
```

### Logs

```bash
ssh root@<droplet-ip>
docker compose -f ~/NTP/docker-compose.yml logs -f trader --tail 200
```

## Deploying Code Changes

```bash
ssh root@<droplet-ip>
cd ~/NTP
git pull
docker compose build trader
# Rebuilds the single shared `ntp-trader:latest` image used by every
# profile-gated trader service (trader / trader-eth / trader-btc /
# trader-sol). The next `up -d` on any profile picks up the new image.

# Single-instrument:
docker compose --profile single up -d trader

# OR per-instrument (one at a time):
docker compose --profile eth up -d trader-eth
docker compose --profile btc up -d trader-btc
docker compose --profile sol up -d trader-sol
# OR all at once:
docker compose --profile multi up -d
```

## One-time hardening upgrade (Redis auth + loopback binding)

If your droplet was set up before the Redis-auth requirement landed (i.e.
your current `.env` has no `REDIS_PASSWORD` and your `docker-compose.yml`
binds Postgres/Redis on `0.0.0.0`), follow this procedure exactly once.
The order matters — wrong order leaves the trader unable to authenticate
to Redis after restart.

```bash
ssh root@<droplet-ip>
cd ~/NTP

# 1. Pull the hardened compose + code
git pull

# 2. Generate a strong Redis password and add it to .env BEFORE restarting
#    anything. The trader reads REDIS_PASSWORD from .env via settings.py.
PASS=$(openssl rand -base64 32)
echo "REDIS_PASSWORD=$PASS" >> .env
# Also confirm POSTGRES_PASSWORD and GRAFANA_PASSWORD are strong.

# 3. Stop EVERYTHING (trader + infra) — clean handover required because
#    the running Redis has no auth, the new one will demand it.
docker compose --profile single --profile eth --profile btc --profile sol --profile multi down

# 4. Recreate Redis with the new password and the loopback-only binding.
docker compose up -d redis postgres grafana
docker compose ps   # all should be healthy

# 5. Sanity-check: confirm Redis now requires auth.
docker exec ntp-redis-1 redis-cli ping
# Expected: (error) NOAUTH Authentication required.
docker exec ntp-redis-1 redis-cli -a "$PASS" --no-auth-warning ping
# Expected: PONG

# 6. Confirm ports are no longer publicly exposed.
ss -tlnp | grep -E '6379|5434'
# Expected: 127.0.0.1:6379 and 127.0.0.1:5434 only — no 0.0.0.0.

# 7. Restart trader(s) with the new compose env. They'll pick up
#    REDIS_PASSWORD via .env / x-trader-env.
docker compose --profile single up -d trader              # or eth/btc/sol/multi
docker logs ntp-trader-eth-1 --since 60s | grep -i 'redis\|connected'
# Expected: connections succeed; no "NOAUTH" errors.
```

After this upgrade, every future deploy follows the standard "code
changes" flow above — REDIS_PASSWORD is sticky in `.env`.

**Forgot to set REDIS_PASSWORD before step 3?** The trader will hard-fail
at `node.build()` with `NOAUTH Authentication required`. Add the env var,
recreate the trader: `docker compose up -d --force-recreate trader-eth`.

## .env-Only Changes (strategy, trade size, instrument)

```bash
ssh root@<droplet-ip>
cd ~/NTP

# Base config (applies to single-instrument + shared defaults):
nano .env
docker compose restart trader              # single-instrument
# OR (per-instrument):
docker compose restart trader-eth trader-btc trader-sol

# Per-instrument override (only affects one container):
nano .env.eth
docker compose restart trader-eth
```

`docker compose restart` does not need a `--profile` flag — compose
remembers the running services. Only `up`-style commands need the flag.

## Wipe Database + Update `.env` + Restart Trading

Use this when you want a clean slate (all historical DB rows removed), then start
trading with updated variables.

```bash
ssh root@<droplet-ip>
cd ~/NTP

# 1) Stop all running traders gracefully first
docker compose stop trader trader-eth trader-btc trader-sol 2>/dev/null
# (compose ignores names that aren't running)

# 2) Wipe PostgreSQL app schema (DESTRUCTIVE: deletes all persisted trading data)
docker compose exec postgres psql -U nautilus -d nautilus_platform \
    -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

# 3) Recreate tables via Alembic migrations
docker compose --profile single run --rm trader alembic upgrade head

# 4) Update variables
nano .env                   # base config
nano .env.eth               # if using multi-instrument

# 5) Restart trading (pick whichever profile you're running)
docker compose --profile single up -d trader
# OR
docker compose --profile eth up -d trader-eth
# OR
docker compose --profile multi up -d
```

Optional sanity checks:

```bash
# Verify schema/tables exist after migration
docker compose exec postgres psql -U nautilus -d nautilus_platform -c "\dt"

# Verify a new run is being registered
docker compose exec postgres psql -U nautilus -d nautilus_platform \
    -c "SELECT id, strategy_id, started_at, stopped_at FROM strategy_runs ORDER BY started_at DESC LIMIT 3;"
```

## Reset Trading Data (Keep Schema) + Update `.env` + Restart Trading

Use this when you want to clear runtime history but keep migrations/schema intact.
This truncates all `public` tables except `alembic_version`.

```bash
ssh root@<droplet-ip>
cd ~/NTP

# 1) Stop all running traders gracefully first
docker compose stop trader trader-eth trader-btc trader-sol 2>/dev/null

# 2) Truncate all app tables, keep alembic_version (schema preserved)
docker compose exec postgres psql -U nautilus -d nautilus_platform -c "
DO \$\$
DECLARE r RECORD;
BEGIN
  FOR r IN
    SELECT tablename
    FROM pg_tables
    WHERE schemaname = 'public' AND tablename <> 'alembic_version'
  LOOP
    EXECUTE format('TRUNCATE TABLE public.%I RESTART IDENTITY CASCADE;', r.tablename);
  END LOOP;
END
\$\$;"

# 3) Update variables (base .env and/or per-instrument files)
nano .env

# 4) Restart trading using the right profile
docker compose --profile single up -d trader        # or --profile eth/btc/sol/multi
```

Optional sanity checks:

```bash
# Expect near-zero rows right after reset/start (except new writes after trader starts)
docker compose exec postgres psql -U nautilus -d nautilus_platform \
    -c "SELECT COUNT(*) AS fills FROM order_fills;"
docker compose exec postgres psql -U nautilus -d nautilus_platform \
    -c "SELECT COUNT(*) AS runs FROM strategy_runs;"
```

## Managing the Containers

Substitute `<svc>` with whichever service is running (`trader`,
`trader-eth`, `trader-btc`, `trader-sol`).

| Action | Command |
|--------|---------|
| Stop trading (graceful) | `docker compose stop <svc>` |
| Start trading | `docker compose start <svc>` (only works if service was previously up) |
| Restart after `.env` change | `docker compose restart <svc>` |
| Restart after code change | `docker compose build trader && docker compose --profile <profile> up -d <svc>` (one build produces the single `ntp-trader:latest` shared by every trader profile) |
| Run migrations (any trader running) | `docker compose exec <svc> alembic upgrade head` |
| Run migrations (no trader running) | `docker compose --profile single run --rm trader alembic upgrade head` |
| Stop all multi-instrument traders | `docker compose --profile multi stop` |
| Check status | `docker compose ps` |
| Check resource usage | `docker stats` |

## Going Live (after Phase 2.5/2.6 paper-trading verification passes)

Phase 2.5 = the cross-gated stack matches backtest behavior end-to-end.
Phase 2.6 = the backtest-vs-paper haircut is documented and within
tolerance. Both gate live deployment. See [`ROADMAP.md`](ROADMAP.md).

Edit `.env` on the droplet:

```bash
HL_TESTNET=false                          # mainnet (testnet uses true)
HL_PRIVATE_KEY=<your mainnet HL key>
HL_WALLET_ADDRESS=<your wallet address>
TRADING_SCRIPT=scripts/run_live.py
LIVE_CONFIRM=yes                          # bypasses input() prompt — required for containerized live
```

For per-instrument deploys, the per-instrument `.env.{asset}` carries
strategy + sizing knobs; live-mode env vars stay in the base `.env`
since they apply to every container.

Then restart whichever profile you're running:

```bash
# Single-instrument
docker compose restart trader

# Per-instrument (each container reads the live-mode vars from base .env)
docker compose restart trader-eth trader-btc trader-sol
```

## Bumping the timescale image

The Postgres named volume `pgdata` MUST be mounted at the path the
running container uses as `PGDATA`. If the mount target doesn't match,
Postgres silently writes to its container writable layer instead of the
volume, and every container removal destroys the database.

This is exactly what bit us 2026-05-15 → 2026-05-22: the compose file
had a stale mount at `/home/postgres/pgdata/data` (older timescale image
convention), the running image's actual PGDATA was
`/var/lib/postgresql/data`, and the hardening PR's `docker compose down`
silently wiped six days of paper-trade history. Regression test at
[`tests/unit/test_compose_pgdata_mount.py`](../tests/unit/test_compose_pgdata_mount.py)
now pins the invariant.

When bumping the `timescale/timescaledb` image version in
`docker-compose.yml`, follow these steps in order:

```bash
# 1. Verify the new image's PGDATA path
docker run --rm timescale/timescaledb:<new-version> env | grep PGDATA
# Expected for 2.x-pg16: PGDATA=/var/lib/postgresql/data
# If different, you MUST update both:
#   - docker-compose.yml `volumes:` line (the mount target)
#   - tests/unit/test_compose_pgdata_mount.py EXPECTED_PGDATA constant

# 2. After bumping in a branch, run the regression test
pytest tests/unit/test_compose_pgdata_mount.py -v

# 3. Stage A smoke test on the droplet (DESTRUCTIVE — wipes pgdata):
#    a fresh `down` + `up -d postgres` cycle must preserve data.
#    Insert a sentinel row before `down`, verify it survives `up`.
```

The compose mount target is the load-bearing line. Get it wrong and
every restart silently destroys your data.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `docker compose up -d` doesn't start any trader | Expected. All traders are profile-gated. Use `--profile single` (legacy) or `--profile eth`/`btc`/`sol`/`multi`. |
| Postgres data gone after `docker compose down` | The `pgdata` volume mount target doesn't match the image's PGDATA. See "Bumping the timescale image" above; check `docker-compose.yml` mounts `pgdata:/var/lib/postgresql/data` and the regression test passes. |
| `env file .env.eth not found` | You're starting a per-instrument profile but haven't copied the template. Run `cp .env.eth.example .env.eth` (or btc/sol) and fill in the values. |
| Trader container restart loop | `docker compose logs <svc> --tail 50`. Common cause: migrations not run. Fix: `docker compose --profile single run --rm trader alembic upgrade head`. |
| Protective stop never fires | After PR #29, `STOP_PCT` (not `DEFAULT_STOP_PCT`) is read at runtime. Verify it's set in `.env` (or `.env.{asset}`) — blank or unset → mixin disabled by design. |
| `ConnectionRefusedError` on postgres | `docker compose ps` — check postgres is healthy. |
| `stopped_at` is NULL after stop | SIGTERM handler issue — verify the runner has `signal.signal(signal.SIGTERM, ...)` at the top of `main()`. |
| Grafana panels empty | Data needs time to accumulate. Check datasource uses `postgres:5432` not `localhost`. For multi-instrument, filter by `(trader_id, strategy_id)` so the three streams don't clobber each other. |
| OOM kills on trader | `docker stats` — single-instrument >1.5GB or multi-instrument total >3GB suggests upgrading to 4GB / 8GB droplet. |
| Live trading restart loop | `input()` needs TTY. Set `LIVE_CONFIRM=yes` in `.env`. |
| Logs not appearing | Verify `PYTHONUNBUFFERED=1` is in Dockerfile. |
| Disk filling up | Log rotation is configured (150MB max per container). Check with `df -h`. |
