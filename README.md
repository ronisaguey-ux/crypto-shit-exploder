# crypto-shit-exploder

Find the Solana wallets that are actually good, paper-trade their moves with
realistic costs, score them on skill rather than luck, and only act when several
of them agree.

It is a research pipeline, not a trading bot: nothing here signs a transaction or
touches a wallet. Every number is a simulation.

## How it works

Four phases, each usable on its own:

1. **Discover** — pull top traders from the Solana leaderboards (SolanaTracker,
   Vybe, Birdeye) and store them as a pool.
2. **Simulate** — replay each wallet's observed swaps through a shadow portfolio
   with pessimistic fills: slippage (fixed or liquidity-derived), an MEV
   adverse-selection tax, an assumed latency, and Solana network fees. A signal
   is not scored until it has survived the simulation.
3. **Score** — a composite fitness per wallet: Sharpe, Sortino, win rate, profit
   factor, and max drawdown, each squashed with `tanh` so no single metric
   dominates. A wallet with too few trades is rejected outright instead of being
   scored on noise.
4. **Aggregate** — treat each surviving wallet as a signal generator. Influence
   is `fitness ** weight_power`, decayed by signal age, then normalized per mint
   and scaled by total conviction. A trade fires only when the aggregate clears
   the confidence threshold — so a few genuinely good wallets can outvote a
   crowd of mediocre ones.

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in the keys you have
```

Python 3.10+.

## Configure

`config/config.yaml` holds every tunable (equity, slippage, fees, scoring
weights, aggregation thresholds). Anything can be overridden by environment
variable, so secrets never have to live in the YAML:

| Variable | Purpose |
| --- | --- |
| `SOLANATRACKER_API_KEY` | SolanaTracker PnL leaderboard |
| `VYBE_API_KEY` | Vybe top-traders |
| `BIRDEYE_API_KEY` | Birdeye per-token top-traders |
| `HELIUS_API_KEY` | Helius webhooks + parsed transaction history |
| `HELIUS_WEBHOOK_SECRET` | Shared secret the webhook receiver expects |
| `CSE_DB_PATH` | SQLite file (default `data/cse.db`) |
| `CSE_CONFIG_PATH` | Alternate YAML config |
| `CSE_<SECTION>__<FIELD>` | Any config value, e.g. `CSE_PAPER__SLIPPAGE_BPS=200` |

At least one discovery provider key is needed to build the pool; Helius is
needed for the live webhook path.

## Usage

```bash
python -m cse discover            # build the trader pool from the leaderboards
python -m cse simulate            # replay stored trades through the paper engine
python -m cse score               # fitness for every trader with history
python -m cse aggregate           # turn fresh signals into decisions
python -m cse report              # shadow-portfolio leaderboard
python -m cse pipeline            # discover -> score -> aggregate
python -m cse webhook             # run the Helius webhook receiver
```

Every command prints JSON, so the output pipes straight into `jq` or another
process.

## Live ingestion

`python -m cse webhook` starts a FastAPI receiver for Helius `enhanced`
webhooks:

- `POST /webhook` — accepts a single transaction or a batch, verifies the
  `Authorization` header against `HELIUS_WEBHOOK_SECRET` when set, parses each
  SWAP into a `Trade`, and stores it.
- `GET /health` — liveness.

Helius Enhanced WebSockets (`transactionSubscribe`) frames are parsed by the same
code path via `cse.ingest.parse_ws_notification`.

## Layout

```
cse/
  models.py          dataclasses: Trader, Trade, Position, ClosedTrade, Signal
  config.py          YAML + CSE_* env loading
  db.py              SQLite persistence (WAL)
  discovery.py       provider fan-out -> trader pool
  ingest.py          Helius webhook / websocket payload -> Trade
  webhook_server.py  FastAPI receiver
  cli.py             argparse entry point (cse/__main__.py enables -m cse)
  providers/         solanatracker, vybe, birdeye, helius
  paper/             slippage, costs, engine (ShadowPortfolio)
  scoring/           composite fitness
  aggregation/       per-mint signal aggregation
config/config.yaml   all tunables
tests/               unit + provider tests (no network)
```

## Tests

```bash
python -m pytest tests/ -q
```

The provider tests drive the real request-building code through
`httpx.MockTransport`, so they assert the URL, params, headers, and body without
touching the network.

## Notes and limits

- **Paper only.** No signing, no keys, no orders.
- Birdeye ranks traders per token, not globally, so it needs `mints=[...]`;
  SolanaTracker and Vybe provide the global leaderboard.
- The simulator is deliberately pessimistic. A wallet that looks good here looks
  better in reality, not worse.
- Results depend on the quality of the discovery data. A wallet with a short,
  lucky history will be rejected by the `min_trades` gate rather than scored.
