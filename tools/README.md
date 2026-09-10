# Verification tools

Standalone scripts used to prove the collector works against live mainnet. They are
not part of the package and are not imported by it; run them from the project root:

```bash
python3 tools/<script>.py
```

Every one of them resolves the project root from its own location, so they keep
working wherever the repository is checked out. None of them needs an API key, and
none of them writes to the real database — they use scratch paths under `/tmp` or
pass an explicit `CSE_DB_PATH`.

## Does it work at all

| Script | What it proves |
| --- | --- |
| `cse_smoke.py` | End-to-end on a scratch DB. Start here. |
| `cse_live_check.py` | The keyless stack reaches real endpoints: RPC health, transactions, decoded trades, prices. |
| `cse_ws_check.py` | Live `logsSubscribe` → notification → fetch → decode. |

## Is the accuracy real

| Script | What it proves |
| --- | --- |
| `cse_enrich_check.py` | Real mainnet swaps yield real pool reserves, real fees, and an honest `slippage_basis` (`exact` only for constant-product venues). |
| `cse_watch_live.py` | The full pipeline on live wallets: filter → queue → enrich → paper-trade → per-trader logs. |

## Can it run for six months

| Script | What it proves |
| --- | --- |
| `cse_soak.py` | Runs the real CLI, SIGKILLs it mid-run, then reopens every store and reports what survived. This is the crash-safety proof. |
| `cse_memdiag.py` | Container-by-container memory census each cycle, to find which structure is growing. |
| `cse_memattr.py` | `tracemalloc` attribution of the growth, to separate a Python object leak from native allocator arenas. |

## Probing the free tier

| Script | What it proves |
| --- | --- |
| `probe_rpc.py` | Which candidate keyless endpoints actually work (several advertised ones are dead or permanently rate-limited). |
| `probe_batch.py` | Whether JSON-RPC batching helps. It does not: the public endpoint rate-limits per method call, and PublicNode rejects batches outright. |
| `bench_rpc.py` | Sustained `getTransaction` throughput of the pool, which sets the wallet ceiling. |
| `seed_wallets.py` | Fills a scratch DB with real active wallets from live mainnet. |

## Notes

- `cse_soak.py` and the memory tools take minutes and hit the network. `SOAK_SECONDS`
  bounds the soak.
- A non-zero `stats.shed` in any output means the pending queue hit its cap and dropped
  the oldest signatures. That is the designed behaviour, not a crash.
