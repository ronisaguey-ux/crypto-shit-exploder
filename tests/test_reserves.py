"""Tests for real execution-cost extraction (cse/reserves.py).

The claim this file defends is "real on-chain numbers, nothing sampled", so the
assertions are mostly arithmetic: the constant-product maths is checked against
hand-computed values, and the reserve reader is checked against a transaction that
deliberately contains a decoy holder and the trader's own moved account.
"""
from __future__ import annotations

import pytest

from cse.models import Side, Trade
from cse.reserves import (
    BASE_FEE_LAMPORTS,
    constant_product_out,
    detect_mev,
    dex_fee_bps,
    effective_bps,
    enrich_trade,
    extract_execution,
    extract_pool_state,
    identify_dex,
    observed_slippage_bps,
    price_impact_bps,
)
from cse.swapdecode import SOL_MINT

WALLET = "Wallet111111111111111111111111111111111111111"
WHALE = "Whale11111111111111111111111111111111111111111"
POOL_AUTH = "PoolAuth1111111111111111111111111111111111111"
MINT = "Mint11111111111111111111111111111111111111111"

RAYDIUM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
CLMM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
UNKNOWN_DEX = "UnknownDex1111111111111111111111111111111111"

SOL_PRICE = 150.0


def _amm_tx(
    *,
    sig: str = "amm-sig",
    base_pre: float = 1_000_000.0,
    base_post: float = 990_000.0,
    quote_pre: float = 100.0,
    quote_post: float = 100.9,
    fee: int = 5_000,
    cu: int = 120_000,
    program: str = RAYDIUM,
    decoy_holder: bool = True,
    err=None,
):
    """A parsed Raydium-style swap: WALLET buys MINT with SOL.

    The pool vaults are the accounts that move; the trader's own token account
    moves too and must be excluded; and (by default) a large untouched holder is
    present to prove it does not inflate the reserve.
    """
    pre_tb = [
        {"mint": MINT, "owner": POOL_AUTH, "accountIndex": 1,
         "uiTokenAmount": {"uiAmount": base_pre}},
        {"mint": SOL_MINT, "owner": POOL_AUTH, "accountIndex": 2,
         "uiTokenAmount": {"uiAmount": quote_pre}},
        {"mint": MINT, "owner": WALLET, "accountIndex": 3,
         "uiTokenAmount": {"uiAmount": 0.0}},
    ]
    post_tb = [
        {"mint": MINT, "owner": POOL_AUTH, "accountIndex": 1,
         "uiTokenAmount": {"uiAmount": base_post}},
        {"mint": SOL_MINT, "owner": POOL_AUTH, "accountIndex": 2,
         "uiTokenAmount": {"uiAmount": quote_post}},
        {"mint": MINT, "owner": WALLET, "accountIndex": 3,
         "uiTokenAmount": {"uiAmount": base_pre - base_post}},
    ]
    if decoy_holder:
        for tb in (pre_tb, post_tb):
            tb.append({"mint": MINT, "owner": WHALE, "accountIndex": 4,
                       "uiTokenAmount": {"uiAmount": 9_000_000.0}})
    return {
        "slot": 500,
        "blockTime": 1_700_000_000,
        "transaction": {
            "signatures": [sig],
            "message": {"accountKeys": [WALLET, POOL_AUTH, program]},
        },
        "meta": {
            "err": err,
            "fee": fee,
            "computeUnitsConsumed": cu,
            "preTokenBalances": pre_tb,
            "postTokenBalances": post_tb,
        },
    }


# ------------------------------------------------------------------ venues
def test_identify_dex_reads_the_program_out_of_the_transaction():
    assert identify_dex(_amm_tx()) == RAYDIUM
    assert identify_dex(_amm_tx(program=CLMM)) == CLMM
    assert identify_dex(_amm_tx(program=UNKNOWN_DEX)) is None


def test_dex_fee_tiers_flag_which_ones_are_the_pools_real_tier():
    # Constant-product venues publish one standard tier, so it is exact.
    assert dex_fee_bps(RAYDIUM) == (25.0, True)
    # Concentrated liquidity sets the tier per pool, so it is only a default.
    assert dex_fee_bps(CLMM) == (25.0, False)
    assert dex_fee_bps(UNKNOWN_DEX) == (0.0, False)
    assert dex_fee_bps(None) == (0.0, False)


# -------------------------------------------------------------------- fees
def test_extract_execution_splits_base_fee_from_priority_fee():
    info = extract_execution(_amm_tx(fee=25_000, cu=140_000), sol_price_usd=SOL_PRICE)
    assert info.total_fee_lamports == 25_000
    assert info.base_fee_lamports == BASE_FEE_LAMPORTS
    assert info.priority_fee_lamports == 20_000
    assert info.compute_units == 140_000
    assert info.n_signatures == 1
    assert info.fee_usd == pytest.approx(25_000 / 1e9 * SOL_PRICE)
    assert info.priority_fee_usd == pytest.approx(20_000 / 1e9 * SOL_PRICE)
    assert info.dex == "raydium_amm"
    assert info.dex_fee_bps == 25.0
    assert info.fee_is_exact is True


def test_extract_execution_never_reports_a_negative_priority_fee():
    # A fee below the base rate is nonsense; clamp to zero rather than go negative.
    info = extract_execution(_amm_tx(fee=1_000), sol_price_usd=SOL_PRICE)
    assert info.priority_fee_lamports == 0
    assert info.priority_fee_usd == 0.0


def test_extract_execution_scales_base_fee_by_signature_count():
    tx = _amm_tx(fee=10_000)
    tx["transaction"]["signatures"] = ["a", "b"]
    info = extract_execution(tx, sol_price_usd=SOL_PRICE)
    assert info.n_signatures == 2
    assert info.base_fee_lamports == 2 * BASE_FEE_LAMPORTS
    assert info.priority_fee_lamports == 0


# ---------------------------------------------------------------- reserves
def test_pool_state_reads_the_real_vaults_and_ignores_decoys():
    pool = extract_pool_state(
        _amm_tx(), MINT, SOL_MINT, wallets=[WALLET], sol_price_usd=SOL_PRICE
    )
    # Exactly the post-trade vault balances: not the 9M decoy holder, and not the
    # 10,000 tokens the trader just received.
    assert pool.reserve_base == pytest.approx(990_000.0)
    assert pool.reserve_quote == pytest.approx(100.9)
    assert pool.mid_price == pytest.approx(100.9 / 990_000.0)
    assert pool.quote_depth_usd == pytest.approx(100.9 * SOL_PRICE)
    assert pool.liquidity_usd == pytest.approx(2 * 100.9 * SOL_PRICE)
    assert pool.model == "constant_product"
    assert pool.confidence == "exact"
    assert pool.dex == "raydium_amm"
    assert pool.usable is True


def test_pool_state_excludes_the_tracked_traders_own_account():
    tx = _amm_tx()
    tracked = extract_pool_state(tx, MINT, SOL_MINT, wallets=[WALLET])
    untracked = extract_pool_state(tx, MINT, SOL_MINT, wallets=[])
    # With the trader untracked their received tokens get counted as "market".
    assert untracked.reserve_base > tracked.reserve_base
    assert untracked.reserve_base == pytest.approx(990_000.0 + 10_000.0)


def test_pool_state_is_unusable_without_moved_vaults():
    tx = _amm_tx()
    tx["meta"]["postTokenBalances"] = tx["meta"]["preTokenBalances"]
    pool = extract_pool_state(tx, MINT, SOL_MINT, wallets=[WALLET])
    assert pool.usable is False
    assert pool.reserve_base == 0.0


def test_clmm_pool_is_labelled_an_estimate_not_exact():
    pool = extract_pool_state(_amm_tx(program=CLMM), MINT, SOL_MINT, wallets=[WALLET])
    assert pool.model == "empirical"
    assert pool.confidence == "estimate"


# ---------------------------------------------------------------- AMM maths
def test_constant_product_out_matches_hand_computed_value():
    # 1e6 * 100 / (1000 + 100) = 90909.0909...
    assert constant_product_out(1000.0, 1_000_000.0, 100.0) == pytest.approx(
        90_909.0909, rel=1e-6
    )


def test_constant_product_out_charges_the_venue_fee_on_the_input():
    # 25 bps of 100 = 99.75 in; 1e6 * 99.75 / (1000 + 99.75).
    assert constant_product_out(1000.0, 1_000_000.0, 100.0, fee_bps=25.0) == pytest.approx(
        1_000_000.0 * 99.75 / 1099.75, rel=1e-9
    )


def test_constant_product_out_is_zero_on_degenerate_input():
    assert constant_product_out(0.0, 1_000.0, 10.0) == 0.0
    assert constant_product_out(1000.0, 1_000.0, 0.0) == 0.0
    assert constant_product_out(1000.0, 1_000.0, -5.0) == 0.0


def test_price_impact_is_exactly_100_percent_when_the_trade_equals_the_reserve():
    # Buying the whole input reserve moves the marginal price 2x, so avg price is
    # half the mid: (mid/avg - 1) * 10_000 = 10_000 bps.
    assert price_impact_bps(1000.0, 1_000_000.0, 1000.0) == pytest.approx(10_000.0)


def test_price_impact_grows_with_size():
    small = price_impact_bps(1000.0, 1_000_000.0, 10.0)
    medium = price_impact_bps(1000.0, 1_000_000.0, 100.0)
    large = price_impact_bps(1000.0, 1_000_000.0, 500.0)
    assert 0 < small < medium < large


def test_price_impact_excludes_the_venue_fee():
    # The function takes fee_bps but must not fold it into the impact number; the
    # fee is reported separately so the two costs stay distinguishable.
    assert price_impact_bps(1000.0, 1_000_000.0, 100.0, fee_bps=25.0) == pytest.approx(
        price_impact_bps(1000.0, 1_000_000.0, 100.0)
    )


def test_effective_bps_walks_the_real_curve_for_a_constant_product_pool():
    pool = extract_pool_state(_amm_tx(), MINT, SOL_MINT, wallets=[WALLET],
                              sol_price_usd=SOL_PRICE)
    bps, basis = effective_bps(pool, notional_usd=1_000.0)
    assert basis == "exact"

    # Derived here from the AMM definition, not from the module's own helper:
    # $1k into a pool holding 990,000 tokens against $15,135 of quote depth.
    depth = 100.9 * SOL_PRICE
    out = 990_000.0 * 1_000.0 / (depth + 1_000.0)
    mid = 990_000.0 / depth
    impact_bps = (mid / (out / 1_000.0) - 1.0) * 10_000.0
    assert bps == pytest.approx(impact_bps + 25.0)  # curve impact plus venue fee
    assert impact_bps > 500.0  # $1k is ~6.6% of this pool, so the impact is large


def test_effective_bps_charges_less_for_a_smaller_copy_trade():
    pool = extract_pool_state(_amm_tx(), MINT, SOL_MINT, wallets=[WALLET],
                              sol_price_usd=SOL_PRICE)
    small, _ = effective_bps(pool, notional_usd=10.0)
    large, _ = effective_bps(pool, notional_usd=5_000.0)
    assert small < large


def test_effective_bps_scales_the_observed_slip_for_a_clmm_pool():
    pool = extract_pool_state(_amm_tx(program=CLMM), MINT, SOL_MINT, wallets=[WALLET],
                              sol_price_usd=SOL_PRICE)
    bps, basis = effective_bps(pool, notional_usd=1_000.0, observed_slippage_bps=100.0)
    assert basis == "estimate"
    assert bps == pytest.approx(100.0 * (1_000.0 / pool.quote_depth_usd) ** 0.5)


def test_effective_bps_is_none_without_a_usable_pool():
    tx = _amm_tx()
    tx["meta"]["postTokenBalances"] = tx["meta"]["preTokenBalances"]
    pool = extract_pool_state(tx, MINT, SOL_MINT, wallets=[WALLET])
    assert effective_bps(pool, 1_000.0) == (0.0, "none")


# ------------------------------------------------------- observed slippage
def test_observed_slippage_is_positive_for_a_buy_above_mid():
    pool = extract_pool_state(_amm_tx(), MINT, SOL_MINT, wallets=[WALLET])
    mid = pool.mid_price
    assert observed_slippage_bps(pool, mid * 1.05, side_is_buy=True) == pytest.approx(
        500.0, rel=1e-6
    )


def test_observed_slippage_is_positive_for_a_sell_below_mid():
    pool = extract_pool_state(_amm_tx(), MINT, SOL_MINT, wallets=[WALLET])
    mid = pool.mid_price
    assert observed_slippage_bps(pool, mid * 0.95, side_is_buy=False) == pytest.approx(
        500.0, rel=1e-6
    )


def test_observed_slippage_is_never_negative_for_a_favourable_fill():
    # A trader who beat the mid is not credited with negative slippage; the copy
    # model must not inherit a rebate that does not exist.
    pool = extract_pool_state(_amm_tx(), MINT, SOL_MINT, wallets=[WALLET])
    assert observed_slippage_bps(pool, pool.mid_price * 0.5, side_is_buy=True) == 0.0


# -------------------------------------------------------------------- MEV
def test_detect_mev_flags_a_wallet_bracketing_the_trade_in_its_slot():
    tx = _amm_tx(sig="victim")
    slot = [
        {"signature": "front", "signer": "attacker", "mints": [MINT]},
        {"signature": "back", "signer": "attacker", "mints": [MINT]},
    ]
    info = detect_mev(tx, slot)
    assert info.same_slot_swaps == 2
    assert info.sandwich_suspect is True
    assert info.notes


def test_detect_mev_does_not_flag_a_merely_busy_slot():
    tx = _amm_tx(sig="victim")
    slot = [
        {"signature": "a", "signer": "w1", "mints": [MINT]},
        {"signature": "b", "signer": "w2", "mints": [MINT]},
    ]
    info = detect_mev(tx, slot)
    assert info.sandwich_suspect is False
    assert info.notes == []


def test_detect_mev_ignores_swaps_on_an_unrelated_mint():
    tx = _amm_tx(sig="victim")
    slot = [
        {"signature": "a", "signer": "attacker", "mints": ["OtherMint"]},
        {"signature": "b", "signer": "attacker", "mints": ["OtherMint"]},
    ]
    assert detect_mev(tx, slot).sandwich_suspect is False


def test_detect_mev_is_silent_without_slot_context():
    info = detect_mev(_amm_tx(), [])
    assert info.same_slot_swaps == 0
    assert info.sandwich_suspect is False


# --------------------------------------------------------------- enrich
def _trade(price: float, amount: float, side: Side = Side.BUY) -> Trade:
    return Trade(trader=WALLET, mint=MINT, side=side, price=price, amount=amount,
                 signature="amm-sig", slot=500, observed_at=1_700_000_000.0)


def test_enrich_trade_attaches_real_pool_fees_and_marks_the_basis_exact():
    tx = _amm_tx(fee=25_000)
    t = enrich_trade(tx, _trade(price=1.1e-4, amount=9_090_909.0),
                     wallets=[WALLET], sol_price_usd=SOL_PRICE)
    assert t.dex == "raydium_amm"
    assert t.slippage_basis == "exact"
    assert t.pool["model"] == "constant_product"
    assert t.pool["confidence"] == "exact"
    assert t.execution["priority_fee_lamports"] == 20_000
    assert t.fees_usd == pytest.approx(25_000 / 1e9 * SOL_PRICE)
    # Depth is reported from the real vault, not the price oracle's guess.
    assert t.pool_liquidity_usd == pytest.approx(100.9 * SOL_PRICE)
    assert t.slippage_bps > 0


def test_enrich_trade_labels_an_unknown_venue_as_an_estimate():
    tx = _amm_tx(program=UNKNOWN_DEX)
    t = enrich_trade(tx, _trade(price=1.1e-4, amount=9_090_909.0), wallets=[WALLET])
    assert t.slippage_basis in {"estimate", "observed"}
    assert t.pool["confidence"] == "estimate"


def test_enrich_trade_says_none_rather_than_inventing_a_number():
    tx = _amm_tx()
    tx["meta"]["postTokenBalances"] = tx["meta"]["preTokenBalances"]
    t = enrich_trade(tx, _trade(price=1.1e-4, amount=9_090_909.0), wallets=[WALLET])
    assert t.slippage_basis == "none"
    assert t.slippage_bps == 0.0
