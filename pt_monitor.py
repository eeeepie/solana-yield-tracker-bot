#!/usr/bin/env python3
"""
ExponentFi Yield Monitor — Telegram bot that monitors PT/YT/LP prices across
ALL Exponent Finance markets.

Scrapes the Exponent Finance page for dehydrated React Query state which
contains every market, then delivers dashboards, percentage-change alerts,
and three always-on default alerts:
  a) New market listed
  b) Any market PT/YT price changes >20%
  c) Any LP pool filled >80%
"""

import csv
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

try:
    from ratex_scraper import fetch_ratex_markets
except Exception as exc:
    logger.warning("Rate-X scraper unavailable: %s", exc)

    def fetch_ratex_markets() -> list[dict]:
        return []

try:
    from spread_signal import evaluate_signals, get_alertable_signals, format_signal_alert, format_signal_summary
except Exception as exc:
    logger.warning("Spread signal module unavailable: %s", exc)
    evaluate_signals = None

# ── Configuration ────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DEFAULT_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

EXPONENT_URL = "https://www.exponent.finance/income/bulksol-20Jun26"

DEFAULT_INTERVAL_SECONDS = 600  # 10 minutes
TICK_INTERVAL_SECONDS = 60

# Default alert thresholds
PCT_CHANGE_THRESHOLD = 0.20  # 20%
POOL_FILL_THRESHOLD = 0.80  # 80%

# ── Data logging ─────────────────────────────────────────────────────────────

DATA_DIR = Path("data")
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
SNAPSHOTS_CSV = DATA_DIR / "snapshots.csv"
ALERTS_CSV = DATA_DIR / "alerts.csv"
SNAPSHOT_INTERVAL = 86400  # 24 hours

DATA_DIR.mkdir(exist_ok=True)
SNAPSHOTS_DIR.mkdir(exist_ok=True)

last_snapshot_ts: float = 0.0

SNAPSHOT_COLUMNS = [
    "timestamp", "source", "address", "platform", "label", "underlying_symbol", "maturity_ts",
    "pt_price", "pt_price_usd", "implied_apy", "pt_implied_apy", "underlying_yield",
    "lp_price", "lp_fees_apy", "tvl_usd", "pool_fill_pct", "pool_cap_usd",
    "base_asset_usd", "sol_price_usd",
]

ALERT_COLUMNS = ["timestamp", "alert_type", "source", "market_address", "label", "detail"]


# Canonical platform names — merges aliases from both Exponent and Rate-X
# so markets for the same project group together in the dashboard.
_PLATFORM_ALIASES = {
    "onrefinance": "onre",
    "solv": "fragmetric",
    "jito restaking": "fragmetric",
}


def _normalize_platform(platform: str) -> str:
    return _PLATFORM_ALIASES.get(platform, platform)


def _log_snapshot(markets: list[dict]) -> None:
    """Append one row per market to snapshots.csv and save daily JSON."""
    now = datetime.now(timezone.utc)
    ts = now.isoformat()

    # CSV snapshot
    write_header = not SNAPSHOTS_CSV.exists()
    with open(SNAPSHOTS_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SNAPSHOT_COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for m in markets:
            row = {
                "timestamp": ts,
                "source": m.get("source", "exponent"),
                "address": m.get("address"),
                "platform": m.get("platform"),
                "label": m.get("label"),
                "underlying_symbol": m.get("underlying_symbol"),
                "maturity_ts": m.get("maturity_ts"),
                "pt_price": m.get("pt_price"),
                "pt_price_usd": m.get("pt_price_usd"),
                "implied_apy": m.get("implied_apy"),
                "pt_implied_apy": m.get("pt_implied_apy"),
                "underlying_yield": m.get("underlying_yield"),
                "lp_price": m.get("lp_price"),
                "lp_fees_apy": m.get("lp_fees_apy"),
                "tvl_usd": m.get("tvl_usd"),
                "pool_fill_pct": m.get("pool_fill_pct"),
                "pool_cap_usd": m.get("pool_cap_usd"),
                "base_asset_usd": m.get("base_asset_usd"),
                "sol_price_usd": m.get("sol_price_usd"),
            }
            writer.writerow(row)

    # Daily JSON snapshot
    json_path = SNAPSHOTS_DIR / f"{now.strftime('%Y-%m-%d')}.json"
    with open(json_path, "w") as f:
        json.dump(markets, f, indent=2, default=str)

    logger.info("Snapshot logged: %d markets → %s + %s", len(markets), SNAPSHOTS_CSV, json_path)


def _log_alert(
    alert_type: str,
    address: str | None,
    label: str | None,
    detail: str,
    source: str | None = None,
) -> None:
    """Append one row to alerts.csv."""
    ts = datetime.now(timezone.utc).isoformat()
    write_header = not ALERTS_CSV.exists()
    with open(ALERTS_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ALERT_COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow({
            "timestamp": ts,
            "alert_type": alert_type,
            "source": source or "",
            "market_address": address or "",
            "label": label or "",
            "detail": detail,
        })


def _load_latest_snapshot_markets() -> tuple[list[dict], Path | None]:
    """Load latest saved snapshot markets from disk as a fallback."""
    snapshot_files = sorted(SNAPSHOTS_DIR.glob("*.json"))
    if not snapshot_files:
        return [], None

    latest_path = snapshot_files[-1]
    try:
        with open(latest_path) as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read snapshot %s: %s", latest_path, exc)
        return [], None

    if not isinstance(payload, list):
        return [], latest_path

    markets: list[dict] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        if not row.get("address"):
            continue
        # Backward compatibility for older snapshots.
        row.setdefault("source", "exponent")
        markets.append(row)

    return markets, latest_path


# ── Scraper ──────────────────────────────────────────────────────────────────


def _extract_sol_price(html: str) -> float | None:
    """Extract SOL USD price from the tokens dehydrated query."""
    m = re.search(
        r"So11111111111111111111111111111111111111112"
        r'.*?priceUsd\\?"\s*:\s*([\d.]+)',
        html[:100000],
    )
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _build_token_map(html: str) -> dict[str, dict]:
    """
    Build a mint address -> {symbol, price_usd} map from the tokens query.
    Tokens are stored as: "<MINT>":{"mint":"<MINT>","symbol":"XXX","priceUsd":N}
    """
    token_map: dict[str, dict] = {}
    for m in re.finditer(
        r'([A-Za-z0-9]{32,50})\\?"\\?:\{\\?"mint\\?"', html
    ):
        mint = m.group(1)
        after = html[m.start():m.start() + 500]
        sym = re.search(r'symbol\\?"\s*:\\?\s*\\?"([^"\\]+)', after)
        price = re.search(r'priceUsd\\?"\s*:\s*([\d.]+)', after)
        if sym:
            entry: dict = {"symbol": sym.group(1)}
            if price:
                try:
                    entry["price_usd"] = float(price.group(1))
                except ValueError:
                    pass
            token_map[mint] = entry
    return token_map


def _extract_market_fields(
    region: str, anchor_offset: int, sol_price_usd: float | None,
    token_map: dict[str, dict] | None = None,
) -> dict | None:
    """Extract market data from a text region.

    The HTML structure per market is:
        [vault: address, platform, mints, maturity ~800 chars]
        [stats: ytImplied, ptImplied, liquidity..., ptPriceInAsset ~1200 chars]

    ptPriceInAsset is at the END of each market's stats block. The anchor_offset
    parameter tells us exactly where ptPriceInAsset sits in the region, so all
    field lookups use proximity to that known position.
    """
    anchor_pos = anchor_offset

    def _find_float(name: str) -> float | None:
        best_val = None
        best_dist = float("inf")
        for m in re.finditer(rf'{name}\\?"\s*:\s*([-\d.eE+]+)', region):
            dist = abs(m.start() - anchor_pos)
            if dist < best_dist:
                try:
                    best_val = float(m.group(1))
                    best_dist = dist
                except ValueError:
                    pass
        return best_val

    def _find_str(name: str) -> str | None:
        best_val = None
        best_dist = float("inf")
        for pat in [
            rf'"{name}"\s*:\s*"([^"]+)"',
            rf'{name}\\?"\s*:\\?\s*\\?"([^"\\]+)',
        ]:
            for m in re.finditer(pat, region):
                dist = abs(m.start() - anchor_pos)
                if dist < best_dist:
                    best_val = m.group(1)
                    best_dist = dist
        return best_val

    def _find_int(name: str) -> int | None:
        best_val = None
        best_dist = float("inf")
        for m in re.finditer(rf'{name}\\?"\s*:\s*(\d+)', region):
            dist = abs(m.start() - anchor_pos)
            if dist < best_dist:
                try:
                    best_val = int(m.group(1))
                    best_dist = dist
                except ValueError:
                    pass
        return best_val

    pt_price = _find_float("ptPriceInAsset")
    if pt_price is None:
        return None

    address = _find_str("address")
    if not address or len(address) < 20:
        return None

    platform = _normalize_platform(_find_str("platform") or "unknown")
    maturity_ts = _find_int("maturityDateUnixTs") or _find_int("maturityUnixTs")
    decimals = _find_int("decimals") or 9

    # Resolve underlying asset (what the frontend shows) and base asset (for USD pricing)
    underlying_mint = _find_str("mintUnderlyingAsset")
    base_mint = _find_str("mintBaseAsset") or _find_str("baseAssetMint")

    underlying_symbol = None
    base_asset_usd = None
    if token_map:
        # Underlying asset symbol = what users see (e.g. BulkSOL, USDC+, stORE)
        if underlying_mint:
            info = token_map.get(underlying_mint)
            if info:
                underlying_symbol = info.get("symbol")
        # Base asset USD price = for converting asset-denominated values to USD
        if base_mint:
            info = token_map.get(base_mint)
            if info:
                base_asset_usd = info.get("price_usd")
    base_asset_symbol = underlying_symbol

    # Determine if this market is expired
    now_ts = int(time.time())
    expired = maturity_ts is not None and maturity_ts < now_ts

    # Derive label: "platform · ASSET · DDMonYY"
    asset_tag = base_asset_symbol or address[:4]
    label = platform or "unknown"
    if maturity_ts:
        try:
            dt = datetime.fromtimestamp(maturity_ts, tz=timezone.utc)
            label = f"{platform} · {asset_tag} · {dt.strftime('%d%b%y').lstrip('0')}"
        except (OSError, ValueError):
            label = f"{platform} · {asset_tag}"
    else:
        label = f"{platform} · {asset_tag}"

    # Liquidity fields — scale by decimals
    scale = 10 ** decimals
    raw_tvl = _find_float("liquidityPoolTvl")
    lp_balance = _find_float("liquidityPoolLpBalance")
    lp_max_supply = _find_float("marketMaxLpSupply")

    # Pool fill: skip if max_supply is a sentinel (<=1) or nonsensical
    pool_fill_pct = None
    if lp_balance and lp_max_supply and lp_max_supply > 1:
        ratio = lp_balance / lp_max_supply
        if ratio <= 10:  # sanity cap — anything >1000% is bad data
            pool_fill_pct = ratio

    lp_price = _find_float("lpPriceInAsset")
    # The frontend displays ytImpliedRateAnnualizedPct as the headline "Implied APY".
    # ptImpliedRateAnnualizedPctIncludingFee is lower (includes protocol fee).
    # Use ytImplied as the primary APY to match the frontend.
    implied_apy = _find_float("ytImpliedRateAnnualizedPct")
    pt_implied_apy = _find_float("ptImpliedRateAnnualizedPctIncludingFee") or implied_apy
    yt_implied_rate = implied_apy
    underlying_yield = _find_float("underlyingYieldsPct")

    # PT/YT USD prices — derived from base asset USD price (from token map)
    pt_price_usd = pt_price * base_asset_usd if base_asset_usd else None

    # TVL in USD
    tvl_usd = None
    if raw_tvl is not None and base_asset_usd is not None:
        tvl_usd = (raw_tvl / scale) * base_asset_usd

    # Pool cap in USD (derived from tvl and fill ratio)
    pool_cap_usd = None
    if tvl_usd is not None and pool_fill_pct and pool_fill_pct > 0:
        pool_cap_usd = tvl_usd / pool_fill_pct

    return {
        "source": "exponent",
        "address": address,
        "platform": platform,
        "maturity_ts": maturity_ts,
        "expired": expired,
        "decimals": decimals,
        "base_asset_mint": base_mint,
        "underlying_symbol": underlying_symbol,
        "base_asset_symbol": base_asset_symbol,
        "pt_price": pt_price,
        "pt_price_usd": pt_price_usd,
        "base_asset_usd": base_asset_usd,
        "implied_apy": implied_apy,
        "pt_implied_apy": pt_implied_apy,
        "yt_implied_rate": yt_implied_rate,
        "underlying_yield": underlying_yield,
        "lp_price": lp_price,
        "lp_fees_apy": _find_float("annualizedLpFeesPct"),
        "tvl_usd": tvl_usd,
        "lp_balance": lp_balance,
        "lp_max_supply": lp_max_supply,
        "pool_fill_pct": pool_fill_pct,
        "pool_cap_usd": pool_cap_usd,
        "label": label,
        "sol_price_usd": sol_price_usd,
    }


def fetch_all_markets() -> list[dict]:
    """
    Fetch the Exponent Finance page and extract ALL active market data from
    the embedded React Server Component dehydrated state.

    Returns a list of active (non-expired) market dicts, or empty list on failure.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }

    try:
        resp = requests.get(EXPONENT_URL, headers=headers, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch Exponent page: %s", e)
        return []

    html = resp.text
    sol_price_usd = _extract_sol_price(html)
    token_map = _build_token_map(html)
    logger.info("Built token map with %d entries", len(token_map))

    all_markets = _parse_all_markets_primary(html, sol_price_usd, token_map)
    if not all_markets:
        all_markets = _parse_all_markets_fallback(html, sol_price_usd, token_map)

    # Manually delisted markets (gone from Exponent UI but still in HTML)
    _DELISTED = {
        "EbQERtzZyMscG4vQcEvZr9qrH856bKcgNo9Dtjcqff9S",  # jupiter · JLP · 8Mar26
    }

    # Filter out expired and delisted markets
    markets = [m for m in all_markets if not m.get("expired") and m.get("address") not in _DELISTED]

    logger.info("Parsed %d active markets (%d expired filtered out)",
                len(markets), len(all_markets) - len(markets))
    return markets


def _parse_all_markets_primary(
    html: str, sol_price_usd: float | None, token_map: dict[str, dict] | None = None,
) -> list[dict]:
    """
    Primary strategy: find all ptPriceInAsset occurrences and parse each
    market entry from the surrounding region.
    """
    markets = []
    seen_addresses = set()

    for match in re.finditer(r'ptPriceInAsset', html):
        pos = match.start()
        # Per-market HTML structure (serialized JSON):
        #   [stats: address, ytImplied, ptImplied, ..., ptPriceInAsset, trailing]
        #   [vault: address, platform, mints, maturityDateUnixTs, ...]
        # Stats fields are ~1200 chars BEFORE ptPriceInAsset.
        # Vault fields are ~200-1200 chars AFTER ptPriceInAsset.
        region_start = max(0, pos - 1300)
        region_end = min(len(html), pos + 1300)
        region = html[region_start:region_end]
        anchor_offset = pos - region_start

        market = _extract_market_fields(region, anchor_offset, sol_price_usd, token_map)
        if market and market["address"] not in seen_addresses:
            seen_addresses.add(market["address"])
            markets.append(market)

    return markets


def _parse_all_markets_fallback(
    html: str, sol_price_usd: float | None, token_map: dict[str, dict] | None = None,
) -> list[dict]:
    """Fallback: smaller extraction windows."""
    markets = []
    seen_addresses = set()

    for match in re.finditer(r'ptPriceInAsset\\?"\s*:\s*([-\d.eE+]+)', html):
        pos = match.start()
        region_start = max(0, pos - 1300)
        region_end = min(len(html), pos + 1300)
        region = html[region_start:region_end]
        anchor_offset = pos - region_start

        market = _extract_market_fields(region, anchor_offset, sol_price_usd, token_map)
        if market and market["address"] not in seen_addresses:
            seen_addresses.add(market["address"])
            markets.append(market)

    return markets


# ── Formatting ───────────────────────────────────────────────────────────────


TELEGRAM_TEXT_LIMIT = 3900


def _source_code(source: str | None) -> str:
    return "RTX" if (source or "").lower() == "ratex" else "EXP"


def _source_tag(source: str | None) -> str:
    return f"[{_source_code(source)}]"


def _market_key(market: dict) -> tuple[str, str]:
    source = (market.get("source") or "exponent").lower()
    address = str(market.get("address") or "")
    return source, address


def _market_display_label(market: dict) -> str:
    return f"{market.get('label', '?')} {_source_tag(market.get('source'))}"


def _fmt_usd(val: float) -> str:
    """Format a USD value compactly: $1.2B, $340M, $52K, $800."""
    if val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.1f}B"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val / 1_000:.0f}K"
    return f"${val:,.0f}"


def _split_text_chunks(text: str, max_len: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split text into Telegram-safe chunks while preserving line boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""

    for line in text.splitlines(keepends=True):
        if len(line) > max_len:
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            for i in range(0, len(line), max_len):
                chunks.append(line[i:i + max_len].rstrip("\n"))
            continue

        if len(current) + len(line) > max_len and current:
            chunks.append(current.rstrip("\n"))
            current = line
        else:
            current += line

    if current:
        chunks.append(current.rstrip("\n"))

    return chunks


def _pool_bar(fill: float | None) -> str:
    """Render a compact pool fill bar."""
    if fill is None:
        return ""
    pct = min(fill * 100, 100)
    filled = min(int(pct / 12.5), 8)
    return "\u2593" * filled + "\u2591" * (8 - filled)


def _pool_info(m: dict) -> str:
    """Format pool line: bar + pct + deployed/cap in USD."""
    fill = m.get("pool_fill_pct")
    if fill is None:
        return ""

    tvl_usd = m.get("tvl_usd")
    cap_usd = m.get("pool_cap_usd")

    bar = _pool_bar(fill)
    pct = f" {fill:.0%}"
    if tvl_usd and cap_usd:
        return f"{bar}{pct} \u2014 {_fmt_usd(tvl_usd)}/{_fmt_usd(cap_usd)}"
    return f"{bar}{pct}"


def format_all_markets_dashboard(markets: list[dict]) -> str:
    """Format all active markets into a Telegram-friendly dashboard."""
    if not markets:
        return "No markets found."

    active = [m for m in markets if not m.get("expired")]
    active.sort(
        key=lambda m: (
            m.get("platform") or "",
            m.get("maturity_ts") or 0,
            _source_code(m.get("source")),
            m.get("address") or "",
        )
    )

    lines = [
        "\U0001f4ca Solana Yield Monitor",
        f"   {len(active)} active markets",
    ]

    current_platform = None
    for m in active:
        platform = m.get("platform") or "?"

        if platform != current_platform:
            # Platform section header
            lines.append("")
            lines.append(f"\u2501\u2501\u2501 {platform.upper()} \u2501\u2501\u2501")
            current_platform = platform

        label = _market_display_label(m)
        pt = m.get("pt_price")
        apy = m.get("implied_apy")
        pt_usd = m.get("pt_price_usd")

        # PT line
        pt_str = f"{pt:.4f}" if pt is not None else "\u2014"
        pt_usd_str = f" (${pt_usd:.2f})" if pt_usd is not None else ""
        apy_str = f" \u00b7 {apy * 100:.1f}% apy" if apy is not None else ""

        # YT line
        yt_asset = 1 - pt if pt is not None else None
        yt_str = f"{yt_asset:.4f}" if yt_asset is not None else "\u2014"
        base_usd = m.get("base_asset_usd")
        yt_usd_str = f" (${yt_asset * base_usd:.2f})" if yt_asset is not None and base_usd else ""
        underlying = m.get("underlying_yield")
        yt_apy_str = f" \u00b7 {underlying * 100:.1f}% yield" if underlying is not None else ""

        # Pool / TVL line
        pool = _pool_info(m)
        tvl_usd = m.get("tvl_usd")

        lines.append(f"\u25b8 {label}")
        lines.append(f"   PT {pt_str}{pt_usd_str}{apy_str}")
        lines.append(f"   YT {yt_str}{yt_usd_str}{yt_apy_str}")
        if pool:
            lines.append(f"   Pool {pool}")
        elif tvl_usd is not None:
            lines.append(f"   TVL {_fmt_usd(tvl_usd)}")

    lines.append("")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"\U0001f552 {now}")

    return "\n".join(lines)


def format_market_detail(m: dict) -> str:
    """Format a single market's detailed view."""
    label = m.get("label", "?")
    source = _source_tag(m.get("source"))

    lines = [
        f"\u250f\u2501\u2501 {label} {source}",
        "\u2503",
    ]

    pt = m.get("pt_price")
    pt_usd = m.get("pt_price_usd")
    base_usd = m.get("base_asset_usd")

    if pt is not None:
        pt_line = f"\u2503  PT Price    {pt:.6f}"
        if pt_usd is not None:
            pt_line += f"  (${pt_usd:.2f})"
        lines.append(pt_line)

    apy = m.get("implied_apy")
    if apy is not None:
        lines.append(f"\u2503  Implied APY {apy * 100:.2f}%")

    pt_apy = m.get("pt_implied_apy")
    if pt_apy is not None and apy is not None and abs(pt_apy - apy) > 0.001:
        lines.append(f"\u2503  PT APY (incl fee) {pt_apy * 100:.2f}%")

    # YT: derived price (1-PT) + underlying yield
    if pt is not None:
        yt_price = 1 - pt
        yt_line = f"\u2503  YT Price    {yt_price:.6f}"
        if base_usd is not None:
            yt_line += f"  (${yt_price * base_usd:.2f})"
        lines.append(yt_line)

    uy = m.get("underlying_yield")
    if uy is not None:
        lines.append(f"\u2503  Underlying  {uy * 100:.2f}%")

    lines.append("\u2503")

    lp = m.get("lp_price")
    if lp is not None:
        lp_line = f"\u2503  LP Price    {lp:.6f}"
        if base_usd is not None:
            lp_line += f"  (${lp * base_usd:.2f})"
        lines.append(lp_line)

    lp_fees = m.get("lp_fees_apy")
    if lp_fees is not None:
        lines.append(f"\u2503  LP Fees     {lp_fees * 100:.2f}%")

    pool = _pool_info(m)
    tvl_usd = m.get("tvl_usd")
    if pool:
        lines.append(f"\u2503  Pool        {pool}")
    elif tvl_usd is not None:
        lines.append(f"\u2503  TVL         {_fmt_usd(tvl_usd)}")

    lines.append("\u2503")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"\u2517\u2501\u2501 {now}")

    return "\n".join(lines)


# ── Global state ─────────────────────────────────────────────────────────────
# In-memory (resets on restart).

all_markets_latest: dict[tuple[str, str], dict] = {}  # (source, address) -> market dict
all_markets_previous: dict[tuple[str, str], dict] = {}  # previous fetch snapshot
known_market_addresses: set[tuple[str, str]] = set()  # for new-market detection
first_fetch_done: bool = False
last_dashboard_sent: dict[int, float] = {}  # chat_id -> timestamp

# chat_id -> list of {"id": int, "pct": float, "market_filter": str|None, "fired": bool}
user_alerts: dict[int, list[dict]] = {}
_next_alert_id: int = 1

# chat_id -> True (subscribed to periodic dashboards)
subscribed_chats: dict[int, bool] = {}


# ── State management ─────────────────────────────────────────────────────────


def _update_market_state(markets: list[dict]) -> list[dict]:
    """
    Rotate latest->previous and detect new market addresses.
    Returns list of newly discovered markets.

    known_market_addresses only grows — a transient scraper failure on one
    source won't shrink it, so markets don't re-appear as "new" on the next
    successful fetch.
    """
    global all_markets_latest, all_markets_previous, known_market_addresses, first_fetch_done

    all_markets_previous = dict(all_markets_latest)

    new_snapshot: dict[tuple[str, str], dict] = {}
    for m in markets:
        key = _market_key(m)
        if key[1]:
            new_snapshot[key] = m
    all_markets_latest = new_snapshot

    new_markets = []
    current_addresses = set(new_snapshot.keys())
    if first_fetch_done:
        newly_found = current_addresses - known_market_addresses
        for key in newly_found:
            new_markets.append(new_snapshot[key])

    # Only grow the known set — never shrink it due to partial fetch failures
    known_market_addresses = known_market_addresses | current_addresses
    first_fetch_done = True

    return new_markets


def _pct_change(old: float | None, new: float | None) -> float | None:
    """Calculate percentage change. Returns None if either value is missing/zero."""
    if old is None or new is None or old == 0:
        return None
    return (new - old) / abs(old)


# ── Alert detection ──────────────────────────────────────────────────────────


def _detect_price_changes() -> list[dict]:
    """Detect markets where PT or YT price changed >8% since last fetch.

    Returns list of dicts with keys: text, alert_type, address, label, detail.
    """
    alerts = []
    for key, current in all_markets_latest.items():
        prev = all_markets_previous.get(key)
        if not prev:
            continue

        address = key[1]
        source = key[0]
        label = _market_display_label(current)

        pt_change = _pct_change(prev.get("pt_price"), current.get("pt_price"))
        if pt_change is not None and abs(pt_change) >= PCT_CHANGE_THRESHOLD:
            direction = "\U0001f53a" if pt_change > 0 else "\U0001f53b"
            text = (
                f"{direction} PT price move \u2014 {label}\n"
                f"   {prev['pt_price']:.4f} \u2192 {current['pt_price']:.4f} ({pt_change:+.1%})"
            )
            alerts.append({
                "text": text, "alert_type": "pt_price_move",
                "source": source, "address": address, "label": label,
                "detail": f"{prev['pt_price']:.4f} -> {current['pt_price']:.4f} ({pt_change:+.1%})",
            })

        apy_change = _pct_change(prev.get("implied_apy"), current.get("implied_apy"))
        if apy_change is not None and abs(apy_change) >= PCT_CHANGE_THRESHOLD:
            direction = "\U0001f53a" if apy_change > 0 else "\U0001f53b"
            prev_apy = prev["implied_apy"] * 100
            cur_apy = current["implied_apy"] * 100
            text = (
                f"{direction} Implied APY move \u2014 {label}\n"
                f"   {prev_apy:.1f}% \u2192 {cur_apy:.1f}% ({apy_change:+.1%})"
            )
            alerts.append({
                "text": text, "alert_type": "apy_move",
                "source": source, "address": address, "label": label,
                "detail": f"{prev_apy:.1f}% -> {cur_apy:.1f}% ({apy_change:+.1%})",
            })

    return alerts


def _detect_pool_fills() -> list[dict]:
    """Detect markets where pool fill crossed above 80% (edge-triggered).

    Returns list of dicts with keys: text, alert_type, address, label, detail.
    """
    alerts = []
    for key, current in all_markets_latest.items():
        fill = current.get("pool_fill_pct")
        if fill is None or fill < POOL_FILL_THRESHOLD:
            continue

        prev = all_markets_previous.get(key)
        prev_fill = prev.get("pool_fill_pct") if prev else None

        if prev_fill is not None and prev_fill >= POOL_FILL_THRESHOLD:
            continue

        address = key[1]
        source = key[0]
        label = _market_display_label(current)
        pct = fill * 100
        text = f"\U0001f7e1 {label}\n  Pool filled to {pct:.1f}%"
        alerts.append({
            "text": text, "alert_type": "pool_fill",
            "source": source, "address": address, "label": label,
            "detail": f"Pool filled to {pct:.1f}%",
        })

    return alerts


def _check_user_alerts(markets_by_addr: dict[tuple[str, str], dict]) -> dict[int, list[str]]:
    """Check user-level percentage alerts. Returns {chat_id: [messages]}."""
    results: dict[int, list[str]] = {}

    for chat_id, alerts in user_alerts.items():
        for alert in alerts:
            if alert["fired"]:
                continue

            pct_threshold = alert["pct"]
            market_filter = alert.get("market_filter")

            for key, current in markets_by_addr.items():
                prev = all_markets_previous.get(key)
                if not prev:
                    continue

                source, addr = key
                label = _market_display_label(current)

                if market_filter:
                    filt = market_filter.lower()
                    if (
                        filt not in label.lower()
                        and filt not in addr.lower()
                        and filt not in source.lower()
                    ):
                        continue

                pt_change = _pct_change(prev.get("pt_price"), current.get("pt_price"))
                if pt_change is not None and abs(pt_change) >= pct_threshold:
                    alert["fired"] = True
                    direction = "\U0001f53a" if pt_change > 0 else "\U0001f53b"
                    msg = (
                        f"{direction} Alert #{alert['id']} triggered!\n"
                        f"{label}: PT {prev['pt_price']:.4f} \u2192 {current['pt_price']:.4f} ({pt_change:+.1%})\n"
                        f"Threshold: {pct_threshold:.0%}"
                    )
                    results.setdefault(chat_id, []).append(msg)
                    break

    return results


# ── Broadcast helper ─────────────────────────────────────────────────────────


async def _broadcast(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Send a message to all subscribed chats + DEFAULT_CHAT_ID."""
    targets = set()
    for cid in subscribed_chats:
        targets.add(cid)
    if DEFAULT_CHAT_ID:
        try:
            targets.add(int(DEFAULT_CHAT_ID))
        except ValueError:
            pass

    for chat_id in targets:
        try:
            for chunk in _split_text_chunks(text):
                await context.bot.send_message(chat_id=chat_id, text=chunk)
        except Exception as e:
            logger.warning("Failed to send to %s: %s", chat_id, e)


# ── Global tick ──────────────────────────────────────────────────────────────


async def _global_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Single global job running every 60s:
    1. Fetch all markets
    2. Update state, detect new markets
    3. Fire default alerts (new market, 20% change, 80% pool)
    4. Log daily snapshots + alert events
    5. Send periodic dashboards (respecting per-user interval)
    6. Check user-level custom alerts
    """
    global last_snapshot_ts
    logger.info("Global tick starting")

    exponent_markets = fetch_all_markets()
    sol_price = next((m.get("sol_price_usd") for m in exponent_markets if m.get("sol_price_usd")), None)
    ratex_markets = fetch_ratex_markets(sol_price_usd=sol_price)
    markets = exponent_markets + ratex_markets
    if not markets:
        logger.warning("Global tick: fetch returned no markets")
        return
    logger.info(
        "Global tick fetched %d Exponent + %d Rate-X markets",
        len(exponent_markets),
        len(ratex_markets),
    )

    new_markets = _update_market_state(markets)

    # Daily snapshot logging
    now = time.time()
    if now - last_snapshot_ts >= SNAPSHOT_INTERVAL:
        try:
            _log_snapshot(markets)
            last_snapshot_ts = now
        except Exception as e:
            logger.error("Failed to log snapshot: %s", e)

    # Default alert: new market
    for m in new_markets:
        label = _market_display_label(m)
        text = (
            f"\U0001f195 New market listed!\n"
            f"{label}\n"
            f"PT Price: {m.get('pt_price', 0):.4f}"
        )
        await _broadcast(context, text)
        _log_alert(
            "new_market",
            m.get("address"),
            label,
            f"PT={m.get('pt_price', 0):.4f}",
            source=m.get("source"),
        )

    # Default alert: 8% price change
    price_alerts = _detect_price_changes()
    if price_alerts:
        header = "\u26a0\ufe0f Large price movements:\n\n"
        text = header + "\n\n".join(a["text"] for a in price_alerts)
        await _broadcast(context, text)
        for a in price_alerts:
            _log_alert(
                a["alert_type"],
                a["address"],
                a["label"],
                a["detail"],
                source=a.get("source"),
            )

    # Default alert: 80% pool fill
    pool_alerts = _detect_pool_fills()
    if pool_alerts:
        header = "\U0001f4a7 Pool fill alerts:\n\n"
        text = header + "\n\n".join(a["text"] for a in pool_alerts)
        await _broadcast(context, text)
        for a in pool_alerts:
            _log_alert(
                a["alert_type"],
                a["address"],
                a["label"],
                a["detail"],
                source=a.get("source"),
            )

    # Cross-venue spread signals
    if evaluate_signals is not None:
        try:
            signals = evaluate_signals(markets)
            alertable = get_alertable_signals(signals)
            if alertable:
                header = "\U0001f4e1 Cross-venue spread alerts:\n\n"
                text = header + "\n\n".join(format_signal_alert(s) for s in alertable)
                await _broadcast(context, text)
                for s in alertable:
                    _log_alert(
                        f"spread_{s.state.lower()}",
                        s.pair_id,
                        s.market_title,
                        f"spread={s.spread_bps:+.1f}bps z={s.z:+.2f} [{','.join(s.reason_codes)}]",
                    )
        except Exception as e:
            logger.error("Spread signal evaluation failed: %s", e)

    # Periodic dashboards
    dashboard_text = None

    for chat_id in list(subscribed_chats.keys()):
        interval = DEFAULT_INTERVAL_SECONDS
        last_sent = last_dashboard_sent.get(chat_id, 0)

        if now - last_sent >= interval:
            if dashboard_text is None:
                dashboard_text = format_all_markets_dashboard(markets)
            try:
                for chunk in _split_text_chunks(dashboard_text):
                    await context.bot.send_message(chat_id=chat_id, text=chunk)
                last_dashboard_sent[chat_id] = now
            except Exception as e:
                logger.warning("Failed to send dashboard to %s: %s", chat_id, e)

    # User-level custom alerts
    user_alert_msgs = _check_user_alerts(all_markets_latest)
    for chat_id, msgs in user_alert_msgs.items():
        for msg in msgs:
            try:
                await context.bot.send_message(chat_id=chat_id, text=msg)
            except Exception as e:
                logger.warning("Failed to send user alert to %s: %s", chat_id, e)
            _log_alert("user_alert", None, None, msg)

    logger.info("Global tick complete — %d markets tracked", len(markets))


# ── Daily report ─────────────────────────────────────────────────────────────


def _build_daily_report(markets: list[dict], signals: list | None = None) -> str:
    """Build a concise daily yield report from current market state."""
    active = [m for m in markets if not m.get("expired")]
    if not active:
        return "\U0001f4ca Daily Report — no active markets."

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = ["\U0001f4c8 Solana Yield \u2014 Daily Report", f"\U0001f552 {now}", ""]

    # ── Top APY opportunities ────────────────────────────────────────────
    with_apy = [m for m in active if m.get("implied_apy") is not None]
    with_apy.sort(key=lambda m: m["implied_apy"], reverse=True)

    lines.append("\U0001f525 Top APY Markets")
    for m in with_apy[:8]:
        apy = m["implied_apy"] * 100
        pt = m.get("pt_price")
        pt_str = f"PT {pt:.4f}" if pt is not None else ""
        label = _market_display_label(m)
        lines.append(f"  {apy:5.1f}%  {label}  {pt_str}")
    lines.append("")

    # ── Pool liquidity highlights ────────────────────────────────────────
    pools = [m for m in active if m.get("pool_fill_pct") is not None]
    pools.sort(key=lambda m: m["pool_fill_pct"], reverse=True)
    filling = [m for m in pools if m["pool_fill_pct"] >= 0.60]
    if filling:
        lines.append("\U0001f4a7 Pools Above 60% Fill")
        for m in filling[:6]:
            fill = m["pool_fill_pct"]
            tvl = m.get("tvl_usd")
            cap = m.get("pool_cap_usd")
            label = _market_display_label(m)
            vol_str = f" \u2014 {_fmt_usd(tvl)}/{_fmt_usd(cap)}" if tvl and cap else ""
            lines.append(f"  {fill:5.0%}  {label}{vol_str}")
        lines.append("")

    # ── TVL leaders (RTX + EXP combined) ─────────────────────────────────
    with_tvl = [m for m in active if m.get("tvl_usd") is not None and m["tvl_usd"] > 0]
    with_tvl.sort(key=lambda m: m["tvl_usd"], reverse=True)
    if with_tvl:
        lines.append("\U0001f4b0 TVL Leaders")
        for m in with_tvl[:6]:
            label = _market_display_label(m)
            lines.append(f"  {_fmt_usd(m['tvl_usd']):>8}  {label}")
        lines.append("")

    # ── Cross-venue spread status ────────────────────────────────────────
    if signals:
        eligible_signals = [s for s in signals if s.eligible]
        active_signals = [s for s in eligible_signals if s.state != "NO_TRIGGER"]
        lines.append("\U0001f4e1 Cross-Venue Spreads")
        if active_signals:
            _emoji = {"ACT": "\U0001f534", "WATCH": "\U0001f7e1", "INFO": "\U0001f535"}
            for s in sorted(active_signals, key=lambda x: {"ACT": 0, "WATCH": 1, "INFO": 2}.get(x.state, 3)):
                direction = "EXP richer" if s.spread_bps > 0 else "RTX richer"
                lines.append(
                    f"  {_emoji.get(s.state, '')} {s.state} {s.market_title}: "
                    f"{s.spread_bps:+.1f} bps (z={s.z:+.1f}) {direction}"
                )
        else:
            lines.append("  All spreads within normal range \u2705")
        for s in eligible_signals:
            if s.state == "NO_TRIGGER":
                lines.append(f"  \u26aa {s.market_title}: {s.spread_bps:+.1f} bps (z={s.z:+.1f})")
        lines.append("")

    # ── Markets expiring soon (within 30 days) ───────────────────────────
    now_ts = time.time()
    expiring = []
    for m in active:
        mat_ts = m.get("maturity_ts")
        if mat_ts and 0 < (mat_ts - now_ts) < 30 * 86400:
            days_left = (mat_ts - now_ts) / 86400
            expiring.append((days_left, m))
    expiring.sort(key=lambda x: x[0])
    if expiring:
        lines.append("\u23f0 Expiring Within 30 Days")
        for days_left, m in expiring[:8]:
            label = _market_display_label(m)
            lines.append(f"  {days_left:4.0f}d  {label}")
        lines.append("")

    # ── Summary stats ────────────────────────────────────────────────────
    exp_count = sum(1 for m in active if (m.get("source") or "").lower() != "ratex")
    rtx_count = sum(1 for m in active if (m.get("source") or "").lower() == "ratex")
    total_tvl = sum(m.get("tvl_usd", 0) for m in active if m.get("tvl_usd"))
    lines.append(f"\u2139\ufe0f {len(active)} markets ({exp_count} EXP, {rtx_count} RTX) \u00b7 Total TVL {_fmt_usd(total_tvl)}")

    return "\n".join(lines)


async def _daily_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: send daily report at 10am UTC+8 (2am UTC)."""
    logger.info("Daily report job triggered")

    if all_markets_latest:
        markets = list(all_markets_latest.values())
    else:
        exp = fetch_all_markets()
        sol = next((m.get("sol_price_usd") for m in exp if m.get("sol_price_usd")), None)
        markets = exp + fetch_ratex_markets(sol_price_usd=sol)
        if markets:
            _update_market_state(markets)

    if not markets:
        logger.warning("Daily report: no markets available")
        return

    signals = None
    if evaluate_signals is not None:
        try:
            signals = evaluate_signals(markets)
        except Exception as e:
            logger.warning("Daily report: signal computation failed: %s", e)

    report = _build_daily_report(markets, signals)
    await _broadcast(context, report)
    logger.info("Daily report sent to %d subscribers", len(subscribed_chats) + (1 if DEFAULT_CHAT_ID else 0))


# ── Telegram command handlers ────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message with usage instructions."""
    msg = (
        "\U0001f4ca *Solana Yield Monitor*\n\n"
        "Real\\-time PT/YT tracking across "
        "[Exponent](https://www.exponent.finance) \\& "
        "[Rate\\-X](https://app.rate-x.io)\\.\n\n"
        "*Auto alerts \\(always on\\):*\n"
        "\u2022 New market listings\n"
        "\u2022 PT/YT price moves \\>20%\n"
        "\u2022 LP pool fill \\>80%\n\n"
        "*Cross\\-venue spread signals:*\n"
        "When the same asset trades on both EXP \\& RTX, "
        "the bot tracks the PT price gap and fires tiered alerts:\n"
        "\U0001f534 `ACT` — statistically extreme spread \\(z \\>\\= 2\\.0\\), likely actionable\n"
        "\U0001f7e1 `WATCH` — moderately extreme or jump detected\n"
        "\U0001f535 `INFO` — mild dislocation, heads\\-up only\n"
        "Signals include spread \\(bps\\), z\\-score, maturity gap class, "
        "and basis regime \\(structural vs normal\\)\\.\n\n"
        "*Daily report \\(10am UTC\\+8\\):*\n"
        "Top APY markets, pool fill highlights, TVL leaders, "
        "cross\\-venue spread status, and expiring markets\\. "
        "Sent automatically to subscribers every morning\\.\n\n"
        "*Commands:*\n"
        "/report — Daily yield report on demand\n"
        "/markets — All\\-markets dashboard\n"
        "/market `filter` — Detail view \\(e\\.g\\. /market hylo\\)\n"
        "/setalert `pct` `[market]` — Custom % alert\n"
        "/alerts — List your alerts\n"
        "/deletealert `id` — Remove an alert\n"
        "/subscribe — Dashboard every 10 min \\+ all alerts\n"
        "/unsubscribe — Stop updates\n"
    )
    await update.message.reply_text(msg, parse_mode="MarkdownV2")


async def cmd_markets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all-markets dashboard. Optional filter argument."""
    await update.message.reply_text("\u23f3 Fetching data...")
    used_snapshot_path: Path | None = None

    if all_markets_latest:
        markets = list(all_markets_latest.values())
    else:
        exp_markets = fetch_all_markets()
        _sol = next((m.get("sol_price_usd") for m in exp_markets if m.get("sol_price_usd")), None)
        markets = exp_markets + fetch_ratex_markets(sol_price_usd=_sol)
        if markets:
            _update_market_state(markets)
        else:
            markets, used_snapshot_path = _load_latest_snapshot_markets()

    if not markets:
        await update.message.reply_text(
            "\u274c Failed to fetch live data and no local snapshot is available."
        )
        return

    # Optional filter
    filt = " ".join(context.args).lower() if context.args else None
    if filt:
        markets = [
            m for m in markets
            if filt in (m.get("label") or "").lower()
            or filt in (m.get("address") or "").lower()
            or filt in (m.get("platform") or "").lower()
            or filt in (m.get("source") or "").lower()
        ]
        if not markets:
            await update.message.reply_text(f"No markets matching '{filt}'.")
            return

    dashboard = format_all_markets_dashboard(markets)
    for chunk in _split_text_chunks(dashboard):
        await update.message.reply_text(chunk)
    if used_snapshot_path:
        await update.message.reply_text(
            f"\u2139\ufe0f Showing cached snapshot from {used_snapshot_path.name} (live fetch unavailable)."
        )


async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detailed view of a single market (or filtered set)."""
    if not context.args:
        await update.message.reply_text("Usage: /market <filter>\nExample: /market bulk")
        return

    filt = " ".join(context.args).lower()
    used_snapshot_path: Path | None = None

    if all_markets_latest:
        markets = list(all_markets_latest.values())
    else:
        exp_markets = fetch_all_markets()
        _sol = next((m.get("sol_price_usd") for m in exp_markets if m.get("sol_price_usd")), None)
        markets = exp_markets + fetch_ratex_markets(sol_price_usd=_sol)
        if markets:
            _update_market_state(markets)
        else:
            markets, used_snapshot_path = _load_latest_snapshot_markets()

    if not markets:
        await update.message.reply_text("\u274c No market data available.")
        return

    matched = [
        m for m in markets
        if filt in (m.get("label") or "").lower()
        or filt in (m.get("address") or "").lower()
        or filt in (m.get("platform") or "").lower()
        or filt in (m.get("source") or "").lower()
    ]

    if not matched:
        await update.message.reply_text(f"No markets matching '{filt}'.")
        return

    for m in matched[:5]:
        detail = format_market_detail(m)
        await update.message.reply_text(detail)

    if len(matched) > 5:
        await update.message.reply_text(f"...and {len(matched) - 5} more. Use a more specific filter.")
    if used_snapshot_path:
        await update.message.reply_text(
            f"\u2139\ufe0f Results are from cached snapshot {used_snapshot_path.name} (live fetch unavailable)."
        )


async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a percentage-change alert. Usage: /setalert 5 [market_filter]"""
    global _next_alert_id
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(
            "Usage: /setalert <pct> [market]\n"
            "Examples:\n"
            "  /setalert 5       \u2014 alert on 5% change in any market\n"
            "  /setalert 3 bulk  \u2014 alert on 3% change in bulk markets"
        )
        return

    try:
        pct = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid percentage. Use a number like 5.")
        return

    if pct <= 0:
        await update.message.reply_text("Percentage must be positive.")
        return

    market_filter = " ".join(context.args[1:]) if len(context.args) > 1 else None

    alert_id = _next_alert_id
    _next_alert_id += 1

    alert = {"id": alert_id, "pct": pct / 100, "market_filter": market_filter, "fired": False}
    user_alerts.setdefault(chat_id, []).append(alert)

    scope = f"markets matching '{market_filter}'" if market_filter else "all markets"
    await update.message.reply_text(
        f"\u2705 Alert #{alert_id} set: notify on {pct:.0f}% price change in {scope}."
    )


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all active (unfired) custom alerts."""
    chat_id = update.effective_chat.id
    alerts = user_alerts.get(chat_id, [])
    active = [a for a in alerts if not a["fired"]]

    if not active:
        await update.message.reply_text(
            "No active custom alerts.\n"
            "Use /setalert to add one.\n\n"
            "Default alerts (new market, 20% price change, 80% pool fill) "
            "are always active for subscribed chats."
        )
        return

    lines = ["\U0001f514 Your Custom Alerts:"]
    for a in active:
        pct_str = f"{a['pct']:.0%}"
        scope = a.get("market_filter") or "all markets"
        lines.append(f"  #{a['id']}  {pct_str} change in {scope}")
    lines.append("")
    lines.append("Use /deletealert <id> to remove one.")
    await update.message.reply_text("\n".join(lines))


async def cmd_deletealert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a specific alert by ID. Usage: /deletealert 3"""
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(
            "Usage: /deletealert <id>\n"
            "Use /alerts to see your alert IDs."
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid ID. Use a number like: /deletealert 3")
        return

    alerts = user_alerts.get(chat_id, [])
    for i, a in enumerate(alerts):
        if a["id"] == target_id:
            alerts.pop(i)
            await update.message.reply_text(f"\U0001f5d1 Alert #{target_id} deleted.")
            return

    await update.message.reply_text(
        f"Alert #{target_id} not found. Use /alerts to see your alerts."
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and send the daily report on demand."""
    await update.message.reply_text("\u23f3 Generating report...")

    if all_markets_latest:
        markets = list(all_markets_latest.values())
    else:
        exp = fetch_all_markets()
        sol = next((m.get("sol_price_usd") for m in exp if m.get("sol_price_usd")), None)
        markets = exp + fetch_ratex_markets(sol_price_usd=sol)
        if markets:
            _update_market_state(markets)

    if not markets:
        await update.message.reply_text("\u274c No market data available.")
        return

    signals = None
    if evaluate_signals is not None:
        try:
            signals = evaluate_signals(markets)
        except Exception as e:
            logger.warning("Report command: signal computation failed: %s", e)

    report = _build_daily_report(markets, signals)
    for chunk in _split_text_chunks(report):
        await update.message.reply_text(chunk)


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Subscribe to periodic dashboards + default alerts."""
    chat_id = update.effective_chat.id
    subscribed_chats[chat_id] = True
    last_dashboard_sent[chat_id] = 0

    await update.message.reply_text(
        f"\u2705 Subscribed! Dashboards every {DEFAULT_INTERVAL_SECONDS // 60} min.\n"
        f"Default alerts (new markets, 20% moves, 80% pool fill) are now active."
    )


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unsubscribe from periodic dashboards."""
    chat_id = update.effective_chat.id
    subscribed_chats.pop(chat_id, None)
    last_dashboard_sent.pop(chat_id, None)
    await update.message.reply_text("\U0001f515 Unsubscribed from periodic updates.")


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    if not BOT_TOKEN:
        logger.error(
            "TELEGRAM_BOT_TOKEN not set. "
            "Copy .env.example to .env and add your token."
        )
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("markets", cmd_markets))
    app.add_handler(CommandHandler("market", cmd_market))
    app.add_handler(CommandHandler("setalert", cmd_setalert))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("deletealert", cmd_deletealert))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))

    app.job_queue.run_repeating(
        _global_tick,
        interval=TICK_INTERVAL_SECONDS,
        first=10,
        name="global_tick",
    )

    # Daily report at 10:00 AM UTC+8 = 02:00 UTC
    from datetime import time as dt_time
    app.job_queue.run_daily(
        _daily_report_job,
        time=dt_time(hour=2, minute=0, second=0, tzinfo=timezone.utc),
        name="daily_report",
    )

    logger.info("Solana Yield Monitor starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
