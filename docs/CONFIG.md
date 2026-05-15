# Configuration: Settings, .env, and Override Patterns

The project has **one** config schema (`src/config/settings.py`) and **one**
override mechanism (`.env`) that flow through the entire system —
backtest notebooks, batch runner, sandbox runner, live runner, validate,
compare-sweeps. Same `.env` deploys to research → paper → live, no
manual re-entry.

## Mental model

| | What | Where |
|---|---|---|
| **`src/config/settings.py`** | The schema. Defines every field, its type, and a default. | In the repo. Edit when adding new fields or changing defaults. |
| **`.env`** | Per-deployment values. Overrides defaults at runtime. | NOT in the repo (`.gitignore`d). Each environment has its own. |
| **`.env.example`** | Template / documentation. Lists every field with example values. | In the repo. Copy to `.env` when setting up a new machine. |

`Settings` is a [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
`BaseSettings` subclass. On `Settings()` construction, three priority
layers combine:

```
1. defaults defined in the class                  (lowest priority)
2. .env file in the cwd (if present)              (override)
3. OS environment variables                       (highest priority)
```

So you can change behaviour without changing code. Edit `.env`, restart
the process, new value takes effect. For one-off experiments, set a
shell env var: `STARTING_CAPITAL=500 python scripts/batch_backtest.py ...`.

## When to edit settings.py vs .env

Day-to-day: edit `.env`. You only edit `settings.py` when:

- **Adding a NEW field** the system didn't have before. Also add it
  to `.env.example` so future deployments know it exists.
- **Renaming a field.** Coordinate with `.env` consumers; consider
  back-compat aliases (we kept `starting_balance` as an alias for
  `starting_capital` to avoid breaking the live runner).
- **Changing the default.** The value used when neither `.env` nor a
  shell env-var sets it.

Examples:

| You want to... | Edit |
|---|---|
| Switch to a $500 account for ongoing testing | `.env` → `STARTING_CAPITAL=500` |
| Run **one** backtest at $500, keep .env untouched | `STARTING_CAPITAL=500 python scripts/batch_backtest.py ...` |
| Ship a $500 default to everyone for the next deploy | `settings.py` (and update `.env.example`) |
| Add a new "max position count" knob | `settings.py` (new field) + `.env.example` (document it) |
| Try a one-off in a notebook | After `settings = get_settings()` in cell 1, write `STARTING_CAPITAL = 500` to shadow that variable for this run |

## Per-system field map

Which system reads which setting:

| Setting | Backtest notebooks | `batch_backtest.py` | `run_sandbox.py` | `run_live.py` | Research notebooks |
|---|:-:|:-:|:-:|:-:|:-:|
| **Account / sizing** |  |  |  |  |  |
| `starting_capital` | ✓ | ✓ | ✓ (`SimulatedExchange.starting_balances`) | ✓ (alive-floor math) | ✓ |
| `trade_notional` | ✓ | ✓ | ✓ | ✓ | — |
| `leverage` | ✓ | ✓ | ✓ | ✓ (exchange config) | — |
| `stop_pct` | ✓ override in cell 1 | ✓ default for `--stop-pcts` | ✓ passed into `MACrossConfig.stop_pct` | ✓ same | — |
| `bootstrap_on_deploy` | — (backtest starts flat, first cross fires naturally) | — | ✓ passed into `MACrossConfig.bootstrap_on_deploy` | ✓ same (set `true` for mid-trend live deploys) | — |
| **Venue / data** |  |  |  |  |  |
| `data_source` | ✓ catalog dir | ✓ | — (live data feed) | — | ✓ |
| `exec_venue` | ✓ fees | ✓ | ✓ adapter | ✓ adapter | — |
| **Strategy + symbol** |  |  |  |  |  |
| `strategy` | — (notebook picks) | — (CLI picks) | ✓ which class to instantiate | ✓ | — |
| `instrument_id` | — (built from `ASSET`) | — (built from `--assets`) | ✓ subscribe + trade | ✓ | — |
| `bar_interval` | — (per-notebook override) | — (per-CLI override) | ✓ data subscription | ✓ | — |
| **Research universe** |  |  |  |  |  |
| `default_assets` | — | ✓ default `--assets` | — | — | ✓ iteration |
| `default_intervals` | — | ✓ default `--intervals` | — | — | ✓ |
| **Strategy hyperparameters** |  |  |  |  |  |
| `ma_fast` / `ma_slow` / `ma_type` | — (notebook picks) | — (CLI picks) | ✓ MACross fast/slow/family | ✓ | — |
| `macross_atr_period` / `..._sl_mult` / `..._tp_mult` | — | — | ✓ MACrossATR bracket sizing | ✓ | — |
| `macdrsi_macd_fast` / `..._slow` / `..._signal` / `..._rsi_period` | — | — | ✓ MACDRSI windows | ✓ | — |
| **Liquidation simulator** |  |  |  |  |  |
| `liquidation_enabled` | ✓ | ✓ | (typically False) | ✗ False (venue handles) | — |
| `liquidation_min_trade_notional` | ✓ | ✓ | ✓ AccountAliveMonitor floor | — | — |
| **Infrastructure** |  |  |  |  |  |
| `postgres_*` | — | — | ✓ PersistenceActor | ✓ | — |
| `redis_*` | — | — | ✓ NT cache | ✓ | — |
| `telegram_*` | — | — | ✓ AlertActor | ✓ | — |
| `hl_private_key` / `hl_wallet_address` | — | — | — (Sandbox exec) | ✓ HL exec config | — |
| `hl_testnet` | — | — | — | ✓ mainnet/testnet switch | — |
| `live_confirm` | — | — | — | ✓ bypass interactive prompt | — |

## Two patterns of `.env` usage

Account/sizing values are usually **consistent across environments** —
$1k / $2k / 20× should flow from research to paper to live untouched.
Same `STARTING_CAPITAL` value in every `.env`.

Infrastructure values **diverge** — `POSTGRES_HOST=localhost` on your
laptop vs `POSTGRES_HOST=db.prod` on the live container. Same field,
different value per `.env`.

## What's NOT in settings (intentionally)

- **Strategy parameter grids** (e.g., `MA_FAST_GRIDS`) — these are
  algorithm-implementation choices, not deployment values. Different
  strategies legitimately have different grids. Live in
  `src/strategies/<name>.py`.
- **Per-sweep fast/slow values, stop_pct in a sweep, asset under test** —
  these vary per run by design. CLI flags or cell-1 lists handle them.
  Note: the **single-value** fast/slow used by the sandbox/live runners
  IS in settings (`MA_FAST`, `MA_SLOW`); only the multi-value grids
  belong to the strategy module.
- **API URLs, fee tiers** — platform constants in
  `src/core/constants.py`. They change when an exchange changes their
  fee schedule, not when you redeploy.

## Override pattern in notebooks

Cell 1 of every backtest notebook reads from settings, then exposes
each value as a local variable that can be shadowed:

```python
from src.config.settings import get_settings

settings         = get_settings()

# Defaults from settings; override below for this run only.
STARTING_CAPITAL = settings.starting_capital   # default: 1000
TRADE_SIZE       = int(settings.trade_notional) # default: 2000
LEVERAGE         = settings.leverage             # default: 20
DATA_SOURCE      = settings.data_source          # default: BINANCE_PERP
EXEC_VENUE       = settings.exec_venue           # default: HYPERLIQUID_PERP
ASSET            = "BTC"                          # backtest-specific
BAR_INTERVAL     = settings.bar_interval          # default: 4h
```

To experiment with smaller capital just for this notebook session, add
one line:

```python
STARTING_CAPITAL = 500   # one-off override; settings.starting_capital still 1000
```

The settings object stays untouched. Other notebooks running
concurrently still see the canonical value.

## How `Settings()` is constructed

```python
@lru_cache
def get_settings() -> Settings:
    return Settings()
```

The `@lru_cache` means the first caller pays construction cost (read
.env, parse env vars, validate types); subsequent calls return the
same instance. If you change `.env` while a long-running process is
up, you need to call `get_settings.cache_clear()` to pick up new
values — but this almost never matters in practice because nothing
changes `.env` mid-run.

For tests and one-off scripts that need pristine defaults regardless
of `.env`:

```python
from src.config.settings import Settings

s = Settings(_env_file=None, postgres_password="test")
```

This bypasses .env entirely.
