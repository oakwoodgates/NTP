# Deploy to Digital Ocean

## Droplet Spec

- **Image:** Docker 1-Click from DO Marketplace (Ubuntu + Docker + Compose pre-installed)
- **Size:** 2GB RAM / 1 vCPU ($12/mo). Upgrade to 4GB ($24/mo) if running multiple strategies or sub-minute bars.
- **Region:** Close to you for SSH latency. Trading latency is irrelevant — hourly bars, not HFT.

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

# 3. Create .env
cp .env.example .env
nano .env
# Fill in: POSTGRES_PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
# Leave TRADING_SCRIPT=scripts/run_sandbox.py (default for paper trading)
# Leave HL_TESTNET=true (paper trading)
# You do NOT need HL_PRIVATE_KEY or HL_WALLET_ADDRESS for paper trading

# 4. Build the trader image (takes a few minutes first time — NT is ~1-2GB)
docker compose build trader

# 5. Start infrastructure ONLY (not trader yet)
docker compose up -d postgres redis grafana

# 6. Wait for postgres to be healthy
docker compose ps    # postgres should show "healthy"

# 7. Run migrations FIRST — before the trader starts
docker compose run --rm trader alembic upgrade head

# 8. Now start the trader
docker compose up -d trader

# 9. Verify all 4 services are running
docker compose ps
# Should show: postgres (healthy), redis (healthy), grafana, trader (running)

# 10. Tail trader logs — should see output immediately (PYTHONUNBUFFERED)
docker compose logs -f trader --tail 50
```

**Migration ordering matters.** The trader calls `_register_run()` on startup, which
INSERTs into `strategy_runs`. If migrations haven't run, the table doesn't exist, the
trader crashes, and enters a restart loop.

## Verify It's Working

```bash
# After a few minutes, check DB writes
docker compose exec postgres psql -U nautilus -d nautilus_platform \
    -c "SELECT COUNT(*) FROM account_snapshots;"
# Should show rows accumulating (one per 60s)

# Check strategy run was registered
docker compose exec postgres psql -U nautilus -d nautilus_platform \
    -c "SELECT id, strategy_id, started_at, stopped_at FROM strategy_runs ORDER BY started_at DESC LIMIT 1;"

# Test graceful shutdown
docker compose stop trader
# Check stopped_at is NOT NULL:
docker compose exec postgres psql -U nautilus -d nautilus_platform \
    -c "SELECT stopped_at FROM strategy_runs ORDER BY started_at DESC LIMIT 1;"
# stopped_at MUST be NOT NULL — if it is NULL, the SIGTERM chain is broken

# Restart
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
docker compose up -d trader    # Restarts only trader, not Postgres/Redis/Grafana
```

## .env-Only Changes (strategy, trade size, instrument)

```bash
ssh root@<droplet-ip>
cd ~/NTP
nano .env
docker compose restart trader  # No rebuild needed
```

## Managing the Container

| Action | Command |
|--------|---------|
| Stop trading (graceful) | `docker compose stop trader` |
| Start trading | `docker compose start trader` |
| Restart after `.env` change | `docker compose restart trader` |
| Restart after code change | `docker compose build trader && docker compose up -d trader` |
| Run migrations (trader running) | `docker compose exec trader alembic upgrade head` |
| Run migrations (trader stopped) | `docker compose run --rm trader alembic upgrade head` |
| Check status | `docker compose ps` |
| Check resource usage | `docker stats` |

## Going Live (after 2+ weeks stable paper trading)

Edit `.env` on the droplet:

```bash
HL_TESTNET=false
HL_PRIVATE_KEY=<your-key>
TRADING_SCRIPT=scripts/run_live.py
LIVE_CONFIRM=yes
```

Then: `docker compose restart trader`

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Trader container restart loop | `docker compose logs trader --tail 50`. Common cause: migrations not run. |
| `ConnectionRefusedError` on postgres | `docker compose ps` — check postgres is healthy. |
| `stopped_at` is NULL after stop | SIGTERM handler issue — check `run_sandbox.py` has the signal handler. |
| Grafana panels empty | Data needs time to accumulate. Check datasource uses `postgres:5432` not `localhost`. |
| OOM kills on trader | `docker stats` — if >1.5GB used, upgrade to 4GB droplet. |
| Live trading restart loop | `input()` needs TTY. Set `LIVE_CONFIRM=yes` in `.env`. |
| Logs not appearing | Verify `PYTHONUNBUFFERED=1` is in Dockerfile. |
| Disk filling up | Log rotation is configured (150MB max). Check with `df -h`. |
