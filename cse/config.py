"""Configuration loading: YAML file + CSE_* environment overrides."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

try:  # optional
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "config.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class DiscoveryConfig:
    target_traders: int = 2000
    providers: list[str] = field(default_factory=lambda: ["solanatracker", "vybe", "birdeye"])
    window_days: int = 30
    pnl_mode: str = "adjusted"
    page_size: int = 1000
    min_volume_usd: float = 50_000
    min_trades: int = 30


@dataclass
class PaperConfig:
    starting_equity_usd: float = 10_000
    position_pct: float = 0.10
    slippage_bps: float = 150
    dynamic_slippage: bool = True
    slippage_jitter: list[float] = field(default_factory=lambda: [0.5, 1.5])
    latency_seconds: float = 3.0
    mev_tax_bps: float = 25
    base_fee_lamports: int = 5000
    priority_fee_lamports: int = 50_000
    lamports_per_sol: int = 1_000_000_000
    sol_price_usd: float = 150.0
    max_liquidity_impact_pct: float = 0.05


@dataclass
class ScoringConfig:
    min_trades: int = 20
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "sharpe": 0.35,
            "sortino": 0.20,
            "win_rate": 0.15,
            "profit_factor": 0.15,
            "max_drawdown": 0.15,
        }
    )
    tanh_scale: float = 2.0
    annualization: float = 8760.0


@dataclass
class AggregationConfig:
    min_fitness: float = 0.15
    confidence_threshold: float = 0.5
    weight_power: float = 2.0
    signal_half_life_hours: float = 6.0
    dedupe_window_seconds: float = 60
    max_open_positions: int = 20


@dataclass
class RpcConfig:
    """Free/keyless Solana RPC pool used to fetch transactions."""

    #: Extra endpoint URLs to add on top of the built-in keyless ones.
    endpoints: list[str] = field(default_factory=list)
    timeout: float = 30.0
    #: Maximum concurrent in-flight getTransaction calls across the whole pool.
    concurrency: int = 16
    #: Requests/second budget for the built-in keyless endpoints.
    public_rps: float = 3.0
    public_heavy_rps: float = 2.0


@dataclass
class WatchConfig:
    """Live `logsSubscribe` watching configuration."""

    #: Each entry: {url, max_connections, subscriptions_per_connection}.
    ws_endpoints: list[dict] = field(
        default_factory=lambda: [
            {
                "url": "wss://api.mainnet-beta.solana.com",
                "max_connections": 4,
                "subscriptions_per_connection": 100,
            }
        ]
    )
    commitment: str = "confirmed"
    #: Drop notifications whose logs show no DEX program before spending a fetch.
    swap_filter: bool = True
    refresh_seconds: float = 300.0
    price_ttl_seconds: float = 300.0
    queue_size: int = 100_000


@dataclass
class Config:
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    aggregation: AggregationConfig = field(default_factory=AggregationConfig)
    rpc: RpcConfig = field(default_factory=RpcConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    db_path: str = "data/cse.db"

    # --- credentials (never logged) ---
    solanatracker_api_key: str = ""
    vybe_api_key: str = ""
    birdeye_api_key: str = ""
    bitquery_api_key: str = ""
    helius_api_key: str = ""
    helius_webhook_secret: str = ""
    alchemy_api_key: str = ""

    def provider_keys(self) -> dict[str, str]:
        return {
            "solanatracker": self.solanatracker_api_key,
            "vybe": self.vybe_api_key,
            "birdeye": self.birdeye_api_key,
            "bitquery": self.bitquery_api_key,
            "helius": self.helius_api_key,
        }


def _apply_env(cfg: Config) -> Config:
    """CSE_<SECTION>__<FIELD> overrides, plus direct credential vars."""
    for name, attr in (
        ("SOLANATRACKER_API_KEY", "solanatracker_api_key"),
        ("VYBE_API_KEY", "vybe_api_key"),
        ("BIRDEYE_API_KEY", "birdeye_api_key"),
        ("BITQUERY_API_KEY", "bitquery_api_key"),
        ("HELIUS_API_KEY", "helius_api_key"),
        ("HELIUS_WEBHOOK_SECRET", "helius_webhook_secret"),
        ("ALCHEMY_API_KEY", "alchemy_api_key"),
        ("CSE_DB_PATH", "db_path"),
    ):
        val = os.getenv(name)
        if val:
            setattr(cfg, attr, val)

    for env_key, env_val in os.environ.items():
        if not env_key.startswith("CSE_") or "__" not in env_key:
            continue
        path = env_key[len("CSE_"):].lower().split("__")
        if len(path) != 2:
            continue
        section, fld = path
        target = getattr(cfg, section, None)
        if target is None or not hasattr(target, fld):
            continue
        current = getattr(target, fld)
        try:
            if isinstance(current, bool):
                parsed: Any = env_val.lower() in ("1", "true", "yes", "on")
            elif isinstance(current, int):
                parsed = int(env_val)
            elif isinstance(current, float):
                parsed = float(env_val)
            elif isinstance(current, list):
                parsed = [x.strip() for x in env_val.split(",") if x.strip()]
            else:
                parsed = env_val
        except ValueError:
            parsed = env_val
        setattr(target, fld, parsed)
    return cfg


def load_config(path: Optional[str | Path] = None) -> Config:
    path = Path(path or os.getenv("CSE_CONFIG_PATH") or DEFAULT_CONFIG)
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}

    cfg = Config(
        discovery=DiscoveryConfig(**(raw.get("discovery") or {})),
        paper=PaperConfig(**(raw.get("paper") or {})),
        scoring=ScoringConfig(**(raw.get("scoring") or {})),
        aggregation=AggregationConfig(**(raw.get("aggregation") or {})),
        rpc=RpcConfig(**(raw.get("rpc") or {})),
        watch=WatchConfig(**(raw.get("watch") or {})),
    )
    return _apply_env(cfg)
