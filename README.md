# crypto-shit-exploder

Find the Solana wallets that are actually good, paper-trade every move they make
with realistic costs, score them on skill rather than luck, and only act when
several of them agree.

It is a research pipeline, not a trading bot: nothing here signs a transaction or
touches a wallet. Every number is a simulation.

**It runs for free.** The whole stack — discovery, live swap watching, pricing —
works with **zero API keys and zero paid tiers**. A free Helius key raises the
watched-wallet ceiling to 5,000; nothing else is required.

## How it works

1. **Discover** — pull top traders from the Solana leaderboards (SolanaTracker,
   Vybe) into a pool. Skipped cleanly when no provider key is configured.
2. **Watch** — subscribe to every pool wallet with `logsSubscribe` (one address
   per subscription), fetch each swap over a pool of keyless RPC endpoints,
   decode it, and store it. This is the live ingest path.
3. **Simulate** — replay each wallet's observed swaps through a shadow portfolio
   with pessimistic fills: slippage (fixed or liquidity-derived), an MEV
   adverse-selection tax, an assumed latency, and Solana network fees. A signal
   is not scored until it has survived the simulation.
4. **Score** — a composite fitness per wallet: Sharpe, Sortino, win rate, profit
   factor, and max drawdown, each squashed with `tanh` so no single metric
   dominates. A wallet with too few trades is rejected outright instead of being
   scored on noise.
5. **Aggregate** — treat each surviving wallet as a signal generator. Influence
   is `fitness ** weight_power`, decayed by signal age, then normalized per mint
   and scaled by total conviction. A trade fires only when the aggregate clears
   the confidence threshold — so a few genuinely good wallets can outvote a
   crowd of mediocre ones.

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

Python 3.10+. No keys are needed to start; `cp .env.example .env` only if you
want to add a free tier for extra headroom.

## Configure

`config/config.yaml` holds every tunable (equity, slippage, fees, scoring
weights, aggregation thresholds, RPC budgets, websocket sharding). Anything can
be overridden by environment variable, so secrets never have to live in the YAML:

| Variable | Purpose | Needed? |
| --- | --- | --- |
| `SOLANATRACKER_API_KEY` | SolanaTracker PnL leaderboard (2,500 req/mo free) | for discovery |
| `VYBE_API_KEY` | Vybe top-traders (25,000 credits/mo free) | for discovery |
| `HELIUS_API_KEY` | Free tier adds 5 WS connections x 1,000 subs = 5,000 wallets | optional |
| `ALCHEMY_API_KEY` | Free tier adds RPC + WS headroom | optional |
| `BIRDEYE_API_KEY` | Not recommended: 30k CU/mo is exhausted in days | no |
| `CSE_DB_PATH` | SQLite file (default `data/cse.db`) | no |
| `CSE_CONFIG_PATH` | Alternate YAML config | no |
| `CSE_<SECTION>__<FIELD>` | Any config value, e.g. `CSE_PAPER__SLIPPAGE_BPS=200` | no |

## Usage

```bash
python -m cse discover            # build the trader pool from the leaderboards
python -m cse watch               # live: watch wallets, paper-trade every trade
python -m cse run                 # discover -> watch -> periodic rescore (the long run)
python -m cse simulate            # replay stored trades through the paper engine
python -m cse score               # fitness for every trader with history
python -m cse aggregate           # turn fresh signals into decisions
python -m cse report              # shadow-portfolio leaderboard
python -m cse pipeline            # discover -> score -> aggregate
python -m cse webhook             # legacy Helius webhook receiver
```

Every command prints JSON, so the output pipes straight into `jq`.

## The free stack, and what it actually costs

Nothing below needs a credit card. The design rule is that no single free tier
has to carry the whole load.

| Layer | Source | Limit | Notes |
| --- | --- | --- | --- |
| Live watch | `logsSubscribe` over WebSocket | 1 address per subscription | Helius free = 5 conns x 1,000 subs = exactly 5,000 wallets |
| Transaction fetch | keyless RPC pool | measured **5.7 fetches/s** | `api.mainnet-beta.solana.com` + two PublicNode hosts |
| Prices | DexScreener | no key, ~300 req/min, 30 mints/call | returns pool liquidity too |
| Prices (fallback) | GeckoTerminal | no key, ~30 req/min | used when DexScreener has no pair |
| Swap parsing | this repo | free | balance-delta decoding, no per-DEX parser |
| Discovery | SolanaTracker / Vybe | 2,500 req/mo / 25k credits/mo | 2 calls/day uses 2.4% / ~24% of budget |

### The one real constraint: fetch throughput

Watching is cheap; **fetching each transaction is not**. Measured on the keyless
pool: **200/200 fetches succeeded at 5.70/s, 0 failures** — about **492,000
transactions/day**.

That sets the wallet ceiling for "paper-trade every single trade":

| Swaps/day per wallet | Max wallets at 100% coverage |
| --- | --- |
| 10 | ~49,000 |
| 20 | ~24,600 |
| 50 | ~9,800 |
| 100 | ~4,900 |
| 500 | ~980 |

So **5,000 real traders trading up to ~100 swaps/day each fits the free budget**.
Bot-grade wallets (500+ swaps/day, e.g. MEV searchers) do not — 5,000 of those
would need ~2.5M fetches/day. Discovery targets traders, not searchers, so this
is usually a non-issue, but it is the number that decides your pool size.

Two cost controls do most of the work:

- **Swap pre-filter.** Notifications whose logs mention no known DEX program are
  dropped before any fetch. Measured live: **5,356 of 7,476 notifications (72%)
  were filtered at zero cost**.
- **Signature dedupe.** A re-delivered notification is free.

### The six-month run

```bash
python -m cse run --target 5000 --maintenance-hours 24
```

This discovers the pool, watches it continuously, and re-scores every 24h so
fitness tracks the current regime rather than a six-month average. State lives in
SQLite (`data/cse.db`), so a restart resumes: `watch_started_at`,
`watch_heartbeat`, `last_rescore_at` and `discovered_total` are all checkpointed
in the `meta` table.

After six months, rank by fitness and copy the survivors:

```bash
python -m cse report --top 50     # the wallets worth following
python -m cse aggregate           # what they collectively say right now
```

To run it as a service, see `deploy/`.

## Live ingestion

`python -m cse watch` is the primary ingest path:

- Shards the wallet list across WebSocket connections
  (`watch.ws_endpoints`: `max_connections` x `subscriptions_per_connection`).
- Fetches each swap over the keyless RPC pool with per-endpoint rate limits and
  automatic rotation when one gets throttled.
- Decodes swaps by diffing `preTokenBalances`/`postTokenBalances` and
  `preBalances`/`postBalances`, so it works on Jupiter, Raydium, Meteora,
  Pump.fun and anything else without a per-DEX parser.
- Prices the fill from the counter leg (SOL or a stablecoin), falling back to
  DexScreener/GeckoTerminal when a trade is token-for-token.
- Feeds the same `PaperTradingEngine` the webhook path uses.

The legacy Helius webhook receiver is still available (`python -m cse webhook`,
`POST /webhook`, `GET /health`) for anyone already on the paid tier.

## Layout

```
cse/
  models.py          dataclasses: Trader, Trade, Position, ClosedTrade, Signal
  config.py          YAML + CSE_* env loading
  db.py              SQLite persistence (WAL)
  discovery.py       provider fan-out -> trader pool
  rpc.py             keyless JSON-RPC pool (rate limits, rotation, dedupe)
  ws.py              sharded logsSubscribe subscription pool
  swapdecode.py      DEX-agnostic balance-delta swap decoding
  prices.py          keyless pricing (DexScreener + GeckoTerminal)
  watcher.py         ws -> fetch -> decode -> paper engine
  runner.py          wiring + the long-running supervisor
  ingest.py          legacy Helius webhook / websocket payload -> Trade
  webhook_server.py  legacy FastAPI receiver
  cli.py             argparse entry point (cse/__main__.py enables -m cse)
  providers/         solanatracker, vybe, birdeye, helius
  paper/             slippage, costs, engine (ShadowPortfolio)
  scoring/           composite fitness
  aggregation/       per-mint signal aggregation
config/config.yaml   all tunables
deploy/              systemd unit + install script
tests/               unit + provider tests (no network)
```

## Tests

```bash
python -m pytest tests/ -q
```

Provider and RPC tests drive the real request-building code through
`httpx.MockTransport`, so they assert URLs, params, headers, and bodies without
touching the network. The decoding tests run on synthetic transactions with
known balance deltas.

## Notes and limits

- **Paper only.** No signing, no keys, no orders.
- Birdeye ranks traders per token, not globally, and its free CU budget dies in
  days; it is supported but not recommended.
- The simulator is deliberately pessimistic. A wallet that looks good here looks
  better in reality, not worse.
- Results depend on the quality of the discovery data. A wallet with a short,
  lucky history will be rejected by the `min_trades` gate rather than scored.
- Free RPC endpoints are shared infrastructure. The configured rates are
  deliberately low; raising them will get you throttled, and the pool will park
  the offender automatically.
