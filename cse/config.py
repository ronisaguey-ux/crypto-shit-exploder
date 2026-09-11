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
    providers: list[str] = field(
        default_factory=lambda: ["solanatracker", "vybe", "keyless", "birdeye"]
    )
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
    #: Fetch workers draining the durable queue, and in-flight fetches per worker.
    workers: int = 4
    #: A feed this quiet is treated as broken, not calm.
    stale_after_seconds: float = 300.0
    #: Read real pool reserves and fees out of each transaction. Costs no extra
    #: RPC call and is what makes the slippage numbers measured rather than fitted.
    enrich: bool = True
    #: Where the per-trader log tree is written.
    log_dir: str = "logs/traders"
    #: Autosave/prune/checkpoint interval. Bounds how much rolling summary a hard
    #: kill can cost; trades themselves are written the moment they are decoded.
    maintenance_seconds: float = 300.0
    #: Delete settled queue rows older than this. Safe because the backfill cursor
    #: never revisits a signature behind it. 0 disables pruning (unbounded growth).
    queue_retention_hours: float = 72.0
    #: Hard ceiling on pending signatures. Past it the oldest are shed and counted
    #: in stats.shed. This exists because the fetch budget is finite: real traders
    #: fit inside it, bots do not, and an uncapped queue would fill the disk.
    queue_max_pending: int = 500_000


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


class ConfigError(ValueError):
    """Raised when a config value is out of range or internally inconsistent.

    The engine used to accept any dict it was handed, so a negative slippage
    limit or an empty RPC pool parsed clean and only failed hours later on the
    wire. Every such value now fails at load, before a socket is opened.
    """


#: Public RPC endpoints throttle hard. Silently falling back to one when no
#: private endpoint is configured produces an engine that starts "fine" and then
#: 429s itself into uselessness; the fallback is refused instead.
_PUBLIC_RPC_HOSTS = {
    "api.mainnet-beta.solana.com",
    "api.devnet.solana.com",
    "api.testnet.solana.com",
}


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def _validate_endpoint(url: str) -> None:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    _require(
        parsed.scheme in ("http", "https", "ws", "wss") and bool(parsed.netloc),
        f"rpc endpoint is not a valid URL: {url!r}",
    )


def validate_config(cfg: Config) -> Config:
    """Fail-fast schema and range validation. Raises ConfigError on any breach."""
    d, p, a, r, w = (
        cfg.discovery,
        cfg.paper,
        cfg.aggregation,
        cfg.rpc,
        cfg.watch,
    )

    # --- discovery ---
    _require(d.target_traders > 0, f"discovery.target_traders must be > 0, got {d.target_traders}")
    _require(d.window_days > 0, f"discovery.window_days must be > 0, got {d.window_days}")
    _require(d.page_size > 0, f"discovery.page_size must be > 0, got {d.page_size}")
    _require(d.min_volume_usd >= 0, "discovery.min_volume_usd must be >= 0")
    _require(d.min_trades >= 0, "discovery.min_trades must be >= 0")

    # --- paper ---
    _require(p.starting_equity_usd > 0, "paper.starting_equity_usd must be > 0")
    _require(0 < p.position_pct <= 1, f"paper.position_pct must be in (0, 1], got {p.position_pct}")
    _require(p.slippage_bps >= 0, f"paper.slippage_bps must be >= 0, got {p.slippage_bps}")
    _require(
        p.slippage_bps <= 10_000,
        f"paper.slippage_bps must be <= 10000 (100%), got {p.slippage_bps}",
    )
    _require(len(p.slippage_jitter) == 2, "paper.slippage_jitter must be [low, high]")
    _require(
        0 <= p.slippage_jitter[0] <= p.slippage_jitter[1],
        f"paper.slippage_jitter must be ascending and non-negative, got {p.slippage_jitter}",
    )
    _require(p.latency_seconds >= 0, "paper.latency_seconds must be >= 0")
    _require(0 <= p.mev_tax_bps <= 10_000, f"paper.mev_tax_bps must be in [0, 10000], got {p.mev_tax_bps}")
    _require(p.base_fee_lamports >= 0, "paper.base_fee_lamports must be >= 0")
    _require(p.priority_fee_lamports >= 0, "paper.priority_fee_lamports must be >= 0")
    _require(p.lamports_per_sol > 0, "paper.lamports_per_sol must be > 0")
    _require(p.sol_price_usd > 0, f"paper.sol_price_usd must be > 0, got {p.sol_price_usd}")
    _require(
        0 < p.max_liquidity_impact_pct <= 1,
        f"paper.max_liquidity_impact_pct must be in (0, 1], got {p.max_liquidity_impact_pct}",
    )

    # --- aggregation ---
    _require(a.min_fitness >= 0, "aggregation.min_fitness must be >= 0")
    _require(0 <= a.confidence_threshold <= 1, "aggregation.confidence_threshold must be in [0, 1]")
    _require(a.weight_power > 0, "aggregation.weight_power must be > 0")
    _require(a.signal_half_life_hours > 0, "aggregation.signal_half_life_hours must be > 0")
    _require(a.max_open_positions > 0, "aggregation.max_open_positions must be > 0")

    # --- rpc ---
    _require(r.timeout > 0, f"rpc.timeout must be > 0, got {r.timeout}")
    _require(r.concurrency > 0, f"rpc.concurrency must be > 0, got {r.concurrency}")
    _require(r.public_rps > 0, "rpc.public_rps must be > 0")
    _require(r.public_heavy_rps > 0, "rpc.public_heavy_rps must be > 0")
    for url in r.endpoints:
        _validate_endpoint(url)
        host = url.split("://", 1)[-1].split("/", 1)[0]
        _require(
            host not in _PUBLIC_RPC_HOSTS,
            f"rpc.endpoints must not list a public throttled endpoint ({host}); "
            "set a private RPC URL or leave it empty",
        )

    # --- watch ---
    _require(w.queue_size > 0, "watch.queue_size must be > 0")
    _require(w.queue_max_pending > 0, "watch.queue_max_pending must be > 0")
    _require(w.workers > 0, f"watch.workers must be > 0, got {w.workers}")
    _require(w.stale_after_seconds > 0, "watch.stale_after_seconds must be > 0")
    _require(w.refresh_seconds > 0, "watch.refresh_seconds must be > 0")
    _require(w.price_ttl_seconds > 0, "watch.price_ttl_seconds must be > 0")
    _require(w.maintenance_seconds > 0, "watch.maintenance_seconds must be > 0")
    _require(w.queue_retention_hours >= 0, "watch.queue_retention_hours must be >= 0")
    _require(w.commitment in ("processed", "confirmed", "finalized"),
             f"watch.commitment must be processed|confirmed|finalized, got {w.commitment!r}")
    for ep in w.ws_endpoints:
        _require(isinstance(ep, dict) and ep.get("url"), f"watch.ws_endpoints entry needs a url: {ep!r}")
        _validate_endpoint(str(ep["url"]))

    # --- credentials / paths ---
    _require(bool(cfg.db_path), "db_path must not be empty")
    _require(
        cfg.helius_webhook_secret != "change-me",
        "helius_webhook_secret is still the shipped sentinel 'change-me'; "
        "set a real secret or the webhook will refuse every request",
    )

    # --- scoring ---
    _require(cfg.scoring.min_trades >= 0, "scoring.min_trades must be >= 0")
    _require(cfg.scoring.tanh_scale > 0, "scoring.tanh_scale must be > 0")
    _require(cfg.scoring.annualization > 0, "scoring.annualization must be > 0")
    if cfg.scoring.weights:
        _require(
            all(v >= 0 for v in cfg.scoring.weights.values()),
            "scoring.weights must all be non-negative",
        )
    return cfg


def load_config(path: Optional[str | Path] = None) -> Config:
    path = Path(path or os.getenv("CSE_CONFIG_PATH") or DEFAULT_CONFIG)
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}

    try:
        cfg = Config(
            discovery=DiscoveryConfig(**(raw.get("discovery") or {})),
            paper=PaperConfig(**(raw.get("paper") or {})),
            scoring=ScoringConfig(**(raw.get("scoring") or {})),
            aggregation=AggregationConfig(**(raw.get("aggregation") or {})),
            rpc=RpcConfig(**(raw.get("rpc") or {})),
            watch=WatchConfig(**(raw.get("watch") or {})),
        )
    except TypeError as exc:
        # An unknown key in config.yaml used to be silently ignored; it is a typo
        # that means the operator's intended setting never applied.
        raise ConfigError(f"config has an unknown or misplaced key: {exc}") from exc

    cfg = _apply_env(cfg)
    return validate_config(cfg)
