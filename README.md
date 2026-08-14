[Uploading README.md…]()
# Index Squeeze Scanner

TTM squeeze breakout scanner for Nifty, Bank Nifty and Sensex, with an options
overlay. It watches index futures on a 5- or 15-minute timeframe, scores each
breakout against nine confluence factors, sizes a hypothetical position, and
tracks it to a target or a stop.

**It places no orders.** Everything it records is paper. Nothing here is
investment advice — verify the numbers, the lot sizes and the strategy itself
before risking money on any of it.

---

## What it does

**Detection.** A Bollinger/Keltner compression of at least 6 bars has to
*release*, then price has to close outside the compression range. Volume on the
breakout bar must beat 1.5× its 20-bar average (the average excludes the
breakout bar itself, or the test grades on a curve).

**Scoring.** Nine factors — direction, squeeze release, volume, ADX, RSI,
market structure, liquidity sweep, OI buildup and higher-timeframe alignment —
each score 0 to 1. Factors in `HARD_FAIL_FACTORS` must be perfect; the rest
feed a weighted composite that has to clear `MIN_COMPOSITE`. The weights are
normalised, so a composite of 0.70 really is 70%.

**Risk.** Stop at 1.5 × ATR, targets at 1/2/3 × ATR, quantity rounded to whole
lots and capped so one trade can't risk more than `MAX_RISK_PER_TRADE_PCT`.

**Management.** A third off at TP1 (stop to breakeven), a third at TP2 (stop to
TP1), the runner trailed by Supertrend(10,3). Everything closes at 15:20.

**Options overlay.** Picks the ATM strike and nearest live expiry, solves
implied volatility from the actual option price with Black-76, ranks that IV
against stored history, and blocks the option leg when premium is rich or theta
would eat the move. See below.

**Tracking.** `/performance` measures what actually happened: equity curve,
how often each exit occurs, whether the composite score predicts anything, and
which filters separate winners from losers. Win rate alone is misleading here —
with targets inside the stop distance you can win most trades and still lose
money, so the page leads with profit factor and expectancy instead.

**Audit.** Every scan writes a row to `scan_logs` with what each check saw and
why it passed or failed. When the scanner is quiet for a day, that table is the
difference between "no setups" and "the feed is broken".

---

## Deploying

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "Index squeeze scanner"
git branch -M main
git remote add origin https://github.com/<you>/index-squeeze-scanner.git
git push -u origin main
```

`.gitignore` already excludes `.env` and `data/`. Check that your credentials
aren't in the commit before you push.

### 2. Create the Railway project

New Project → Deploy from GitHub repo → pick the repo.

The repo ships a **Dockerfile**, and `railway.json` selects it. This is
deliberate: an earlier deploy crash-looped on
`ModuleNotFoundError: No module named 'uvicorn'` because the Nixpacks build
copied the source to `/app` and started it without ever running pip install.
The Dockerfile verifies every import at build time, so a missing dependency
fails the build instead of producing an image that dies on every boot.

If the service was already created and is still set to Nixpacks, change it:
**Service → Settings → Build → Builder → Dockerfile**, then redeploy.
`nixpacks.toml` is included as a fallback with the install phase spelled out
explicitly, should you prefer that route.

### 2b. Two services, one image

Create a second service from the same repo for the scanner:

| | Web service | Worker service |
|---|---|---|
| `RUN_MODE` | *(unset)* | `worker` |
| `RUN_SCHEDULER` | `false` | `true` |
| Everything else | same | same |

Both run the same image and the same `/health` path — the worker exposes a
minimal health endpoint reporting scheduler state, registered jobs, signal
budget and stream status, so Railway's health check passes and you can see what
the scanner is doing without reading logs. Point both at the same
`DATABASE_URL`.

Running the worker is optional. With a single service and `RUN_SCHEDULER=true`,
the web process does the scanning too.

### 3. Add Postgres — do this before you care about the data

In the project: New → Database → Add PostgreSQL. Railway injects `DATABASE_URL`
automatically and the app picks it up.

Without it the app falls back to SQLite in the container filesystem, which
Railway wipes on **every redeploy**. Signals, IV history and scan logs all go
with it. IV rank in particular needs weeks of history to mean anything, so
losing it repeatedly makes that filter permanently useless.

### 4. Set environment variables

Copy from `.env.example` into Railway's Variables tab. Minimum to get a page up:

```
FEED_MODE=mock
```

That runs on synthetic candles with no broker at all. Deploy it that way first
and confirm the dashboard renders, then switch to live data:

```
FEED_MODE=5paisa
FIVEPAISA_APP_NAME=...
FIVEPAISA_APP_SOURCE=...
FIVEPAISA_USER_ID=...
FIVEPAISA_PASSWORD=...
FIVEPAISA_USER_KEY=...
FIVEPAISA_ENCRYPTION_KEY=...
FIVEPAISA_CLIENT_CODE=...
FIVEPAISA_PIN=...
FIVEPAISA_TOTP_SECRET=...      # base32 seed, not a 6-digit code
```

`FIVEPAISA_TOTP_SECRET` is the seed from 5paisa's TOTP setup screen. Store the
seed rather than a code — the app generates a fresh one at every login, so
sessions survive restarts and redeploys.

### 5. Lock the dashboard

Railway URLs are public. Set `DASHBOARD_TOKEN` to something long and open the
site as `https://your-app.up.railway.app/?token=...` once; it sets a cookie.
`/health` stays open so the platform health check keeps working.

---

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # FEED_MODE=mock needs nothing else
python main.py                # http://localhost:8000
```

Run the offline test suite after touching anything in `indicators.py`,
`filters.py` or `options_layer.py`:

```bash
python selftest.py
```

It exercises the whole pipeline on synthetic candles — no broker, no network —
and includes a hand-built textbook setup that *must* produce a signal. If a
change breaks the squeeze or sweep logic, this catches it in seconds instead of
during market hours.

---

## Universe, streaming and the daily cap

**Dynamic equity universe.** Rebuilt at 08:30 each morning from the scrip
master: stock futures only, anything under `MIN_STOCK_PRICE` dropped, anything
under `MIN_TURNOVER_CR` of daily traded value dropped, then ranked on a blend of
turnover and momentum. Turnover rather than share volume, because 10 lakh shares
of a Rs 150 stock and 10 lakh of a Rs 3,000 stock are not comparable liquidity.
Momentum uses an N-day rate of change once daily closes exist and falls back to
today's move before then, recording which it used rather than quietly scoring
zero.

**Index isolation.** Indices and stocks run separate thresholds — stocks gap
harder and reverse faster, so they get their own volume multiple, ADX bar,
composite bar and stop width (`EQ_*` settings) rather than inheriting index
tuning.

**WebSocket streaming.** One morning handshake, then a single connection
replaces per-cycle polling. Three traps are handled explicitly: ticks carry
*cumulative* day volume, so per-bar volume is a difference of that counter, not
a sum (summing would inflate every bar and the volume gate is the primary hard
filter); streamed bars are spliced onto REST backfill with the stream winning
any overlapping timestamp, so a bar is never counted twice; and a silent socket
is treated as stale rather than as a quiet market.

**Strict cap.** Hard ceiling of `MAX_SIGNALS_PER_DAY`. When a scan produces more
setups than remaining budget they are ranked by score and the rest deferred, so
the last slot doesn't go to whichever symbol happened to be scanned first. On
exhaustion the stream unsubscribes. The count is read from the ledger, not an
in-process counter — a counter resets on redeploy and a container restarting at
noon would happily fire five more.

**Decoupled processes.** `Procfile` defines both `web` and `worker`. Run the
worker as a second Railway service against the same `DATABASE_URL` with
`RUN_MODE=worker`, and set `RUN_SCHEDULER=false` on the web service so jobs fire
in exactly one place and a dashboard request can never slow a scan.

---

## API budget

Closed candles never change, so they are stored in the database and re-read
instead of re-downloaded. Two further changes cut the call *count*:

- **The 15-minute series is derived from stored 5-minute bars** (`DERIVE_HTF`),
  removing one request per symbol per cycle. It also removes a bug class:
  independently fetched 5m and 15m series can disagree at the boundary, so the
  trend filter ends up judging a slightly different market than the trigger.
- **Open interest is only fetched for symbols that pass stage one** — a squeeze
  release within the last few bars. That is about 2% of symbol-scans.

| | per cycle | per day | at 3 calls/s |
|---|---|---|---|
| 3 symbols, before | 9 | 675 | 3s |
| 3 symbols, after | 3 | 230 | 1s |
| 200 symbols, before | 600 | 45,000 | 200s |
| 200 symbols, after | 204 | 15,300 | 68s |

A cycle is 300 seconds, so 200 symbols goes from roughly 1.5× headroom to 4×.

Note that `MAX_CONCURRENT_FETCHES` and `API_CALLS_PER_SECOND` limit different
things. A semaphore caps how many calls run *at once*; it does nothing to stop
200 symbols firing 200 requests in two seconds as each one completes. Broker
limits are calls per interval, so `RateLimiter` in `feed_base.py` enforces that
separately.

`/health` reports the store's hit rate. A pre-market backfill at 08:45 loads
history before the open so the first scan isn't competing with live trading for
the budget, and `prune()` drops bars older than `CANDLE_RETENTION_DAYS`.

---

## The options overlay

Enabled by `OPTIONS_ENABLED`. For every signal it builds a plan:

| Field | Meaning |
|---|---|
| strike / type | ATM strike, CE for longs and PE for shorts |
| expiry, DTE | nearest live expiry; rolls forward on expiry day past the cutoff |
| IV | solved from the live option price with Black-76, not assumed |
| IV rank | where that IV sits in its own 90-day range, 0–100 |
| delta, theta | Black-76 greeks on the chosen contract |
| theta drag | decay over `EXPECTED_HOLD_HOURS`, as a % of the expected move to TP1 |

Three gates can block the option leg:

1. **Expiry-day theta cutoff.** No new option entries after `THETA_CUTOFF_TIME`
   on the day the contract expires. With `EXPIRY_DAY_ROLLOVER=true` it moves to
   the next expiry instead of refusing outright.
2. **IV rank band.** Blocks above `MAX_IV_RANK` (you'd be buying rich premium,
   and a vol crush can lose money on a correct directional call) and below
   `MIN_IV_RANK` (the market is pricing no movement).
3. **Theta drag.** Blocks when decay over a realistic hold costs more than
   `MAX_THETA_DRAG_PCT` of what the option should make if the underlying reaches
   TP1.

`OPTIONS_BLOCK_SIGNAL=false` (the default) means a blocked option leg leaves the
futures-level signal standing, flagged with the reason. Set it `true` to drop
the signal entirely.

**Two things to be honest about.**

*IV rank starts out unavailable.* It needs `IV_RANK_MIN_SAMPLES` readings across
`IV_RANK_MIN_DAYS` distinct days. Until then it returns nothing and the band is
not applied — the signal is labelled "rank pending" rather than silently
passing. A background job samples ATM IV every `IV_SAMPLE_MINUTES` during market
hours so history accumulates even on days with no setups. Expect a few weeks
before the filter does anything, and don't lose the database in the meantime.

*Why the per-day theta ceiling is off by default.* A weekly ATM option burns
roughly 7% of its premium per day at 7 DTE, 30% at 2 DTE and 72% at 1 DTE. A
flat "%/day" cap therefore rejects almost every weekly option. But this scanner
squares off intraday, so a position held two hours only pays a fraction of a
day's decay — at 1 DTE that same contract costs about 15% of the expected move,
not 72%. `MAX_THETA_DRAG_PCT` measures the cost you actually bear;
`MAX_THETA_PCT_PER_DAY` is left at 0 (disabled) and is there if you start
holding overnight.

---

## Troubleshooting

**`ModuleNotFoundError` on boot, container restarting in a loop.** The build
never installed dependencies. Confirm the service builder is set to Dockerfile
(Settings → Build), not Nixpacks. The build log should contain
`all runtime dependencies present` — if that line is absent, the image was
built without the install step.

**Worker service fails its health check.** It should not, since worker mode
serves `/health` on `$PORT`. If it does, confirm `RUN_MODE=worker` is set on
that service and that no start command overrides the image `CMD`.

**Both services scanning, duplicate signals.** Set `RUN_SCHEDULER=false` on the
web service. The signal cap is read from the database, so duplicates are capped
rather than unbounded, but both processes doing the work wastes API budget.

**Everything works but no signals appear.** Check `/api/logs` — every scan
records why each symbol was rejected. Silence there means the feed is down;
rejection reasons mean the filters are working as designed.

---

## Things to verify before going live

- **Lot sizes** (`LOT_SIZES`) change with exchange circulars. Check the current
  contract note; the defaults will go stale.
- **The Sensex cash token** in `feed_5paisa.py` and the index scrip codes are
  worth confirming once against the scrip master. A wrong token doesn't error —
  it silently returns a different instrument's candles.
- **P&L is tracked on futures movement, not option premium.** An option's actual
  result depends on delta, theta and vega together, so the ledger's numbers will
  not match an option position tick for tick. Treat them as a measure of whether
  the *signal* was right, not of what the trade would have paid.
- **Trading holidays aren't encoded.** `MarketClock.HOLIDAYS` is an empty set;
  add dates from the exchange circular or the scanner will run on holidays.
- **`py5paisa` signatures.** The feed matches 0.7.x. If 5paisa ships a breaking
  change, check with:
  ```bash
  python -c "import py5paisa, inspect; print(inspect.signature(py5paisa.FivePaisaClient.historical_data))"
  ```

---

## Layout

```
main.py            entry point, IST scheduler, resilient startup
config.py          every setting, env-driven
clock.py           market sessions, naive-IST helpers
database.py        models, Postgres/SQLite switch, in-place column migration
analytics.py       performance stats, outcome mix, factor post-mortem
universe.py        daily F&O universe selection and ranking
feed_ws.py         websocket ticks, bar construction, unsubscribe
signal_budget.py   strict daily cap and exhaustion hook
candle_store.py    stored OHLCV, incremental fetch, HTF resampling
indicators.py      ATR, RSI, ADX, Bollinger, Keltner, squeeze, Supertrend
filters.py         the nine-factor scoring engine
scanner.py         concurrent scans, OI tracking, cooldowns
signal_engine.py   sizing, scale-outs, trailing, square-off
options_layer.py   Black-76, IV solving, IV rank, theta gates
feed_base.py       the interface a broker feed has to satisfy
feed_5paisa.py     live data
feed_mock.py       synthetic data, no broker needed
scrip_resolver.py  futures contract resolution across expiries
notifier.py        Telegram alerts, optional
dashboard.py       FastAPI routes and JSON API
selftest.py        offline regression suite
templates/         the dashboard page
```

## Endpoints

| Route | Purpose |
|---|---|
| `/` | dashboard |
| `/health` | status, feed state, last scan — Railway's health check |
| `/api/scan-now` | run a scan immediately |
| `/api/signals` | recent signals as JSON |
| `/api/logs` | scan log with rejection reasons |
| `/api/equity` | cumulative paper P&L |
| `/api/stats` | today's summary |
| `/performance` | P&L tracker: equity curve, outcome mix, factor post-mortem |
| `/api/performance` | the same report as JSON |
| `/api/equity-curve` | cumulative P&L with running drawdown |
| `/api/export.csv` | full ledger download |
| `/api/square-off` | close every open position now |

`/health` also reports candle-store hit rate and API calls made vs saved.
