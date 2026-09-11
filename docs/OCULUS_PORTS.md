# Reusable pieces from `oculus` → crypto-shit-exploder

Reviewed 2026-09-11. Ordered by value to the money path, not by how clever they are.
Every entry names the source file so it can be read before it is copied.

## 1. `assert_slippage_within` — pre-trade mark-price sanity gate
**Source:** `oculus/live/execute_trade.py` (test: `oculus/tests/live/test_execute_trade_slippage_guard.py`)

Rejects an order when the mark price deviates from the intended price by more than
a bps budget, and **fails closed on a zero mark** instead of dividing by zero. cse
has no equivalent: `cse/reserves.py` computes slippage but never *refuses* a fill
that is implausible. A decoded trade whose price is 10x off the pool mid is
currently accepted and paper-filled.

**Port as:** `cse/reserves.py::assert_slippage_within(intended, mark, max_bps)` and
call it in `enrich_trade` before the basis is set. Straight copy — 20 lines of pure
arithmetic with no oculus imports.

## 2. `CircuitBreaker` — trips on drawdown OR error rate
**Source:** `oculus/live_risk_manager.py` (62 lines, stdlib only)

Two independent trip conditions (equity drawdown from peak, and error rate over a
sliding window), with trip state and reason readable. cse's `guards.py` has a
`KillSwitch` engaged manually and a `StalenessGuard`, but nothing watching the
*error rate* of the ingest path.

**Port as:** `cse/guards.py::CircuitBreaker` fed by `Watcher.stats` — the watcher
already counts `fetch_errors`/`decode_errors`, so the wiring is a counter in, a
boolean out.

## 3. `RiskManager.on_bar` — drawdown kill semantics
**Source:** `oculus/risk_manager.py` (248 lines)

Kill is an **OR** between drawdown-from-initial (10%) and daily drawdown (5%),
with the daily base reset at session start and a session-date-aware `bars_per_day`
cache so a shortened session never reuses a stale barrier. cse's `DrawdownGuard`
exists but is instantiated nowhere (audit F-013).

**Port as:** replace cse's unused `DrawdownGuard` with this shape. The
session-date reset is the part worth stealing — it is the bug cse would otherwise
reintroduce.

## 4. `ExitLogicDispatcher` — drawdown-guarded exits
**Source:** `oculus/exit_logic_dispatcher.py` (172 lines)

Routes an exit id to a handler and checks a drawdown guard **before** executing.
cse closes a position on any SELL (audit F-020: a partial sell closes the whole
position). This is the missing "should this exit actually fire" layer.

## 5. Mark-to-market
**Source:** `oculus/live/data_models.py` (`unrealized_pnl`),
`oculus/backtester/orderbook_backtest.py` (`Fill`, `OrderBook.mark_to_market`)

cse marks equity only on close, so an open position contributes nothing and a
buy-and-hold trader looks like a loser until the round trip completes. oculus
carries `unrealized_pnl` on the position and has a `mark_to_market(qty,
avg_price_ticks)` helper. Porting the field plus one mark call fixes the second
half of F-020.

## What NOT to port
- **`live/ftrom.py`** (603 lines, 10,000 weight configs + numba JIT). A
  signal-combination engine for a different problem (per-config net-edge
  prediction). cse's `aggregation/aggregator.py` already does weighted voting;
  importing FTROM would add numba as a hard dependency for no measured gain.
- **`orderbook_backtest.py`'s tick-based `OrderBook`.** cse works in USD per token
  from decoded balances, not integer price ticks on a synthetic book. Adopting the
  tick model would require rewriting `reserves.py`.
- **Anything importing `oculus.config_singleton`** — a process-wide singleton, and
  cse's config is explicitly per-call (`load_config()`).

## Order to do them in
1. `assert_slippage_within` — cheap, closes a real hole, no deps
2. `CircuitBreaker` on the watcher counters
3. drawdown kill semantics into `guards.py`
4. mark-to-market + partial-sell sizing
5. `ExitLogicDispatcher` — only if partial exits are actually wanted
