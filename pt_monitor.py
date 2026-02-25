#!/usr/bin/env python3
"""
ExponentFi Yield Monitor — Telegram bot that monitors PT/YT/LP prices across
ALL Exponent Finance markets.

Scrapes the Exponent Finance page for dehydrated React Query state which
contains every market, then delivers dashboards, percentage-change alerts,
and three always-on default alerts:
  a) New market listed
  b) Any market PT/YT price changes >8%
  c) Any LP pool filled >80%
"""

import logging
import os
import re
import time
from datetime import datetime, timezone

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

# ── Configuration ────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DEFAULT_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

EXPONENT_URL = "https://www.exponent.finance/income/bulksol-20Jun26"

DEFAULT_INTERVAL_SECONDS = 600  # 10 minutes
TICK_INTERVAL_SECONDS = 60

# Default alert thresholds
PCT_CHANGE_THRESHOLD = 0.08  # 8%
POOL_FILL_THRESHOLD = 0.80  # 80%

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

    platform = _find_str("platform")
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

    # Filter out expired markets
    markets = [m for m in all_markets if not m.get("expired")]

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


def _fmt_usd(val: float) -> str:
    """Format a USD value compactly: $1.2B, $340M, $52K, $800."""
    if val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.1f}B"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val / 1_000:.0f}K"
    return f"${val:,.0f}"


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
    active.sort(key=lambda m: (m.get("platform") or "", m.get("maturity_ts") or 0))

    lines = [
        "\U0001f4ca ExponentFi Yield Monitor",
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

        label = m.get("label", "?")
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

        # Pool line
        pool = _pool_info(m)

        lines.append(f"\u25b8 {label}")
        lines.append(f"   PT {pt_str}{pt_usd_str}{apy_str}")
        lines.append(f"   YT {yt_str}{yt_usd_str}{yt_apy_str}")
        if pool:
            lines.append(f"   Pool {pool}")

    lines.append("")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"\U0001f552 {now}")

    return "\n".join(lines)


def format_market_detail(m: dict) -> str:
    """Format a single market's detailed view."""
    label = m.get("label", "?")

    lines = [
        f"\u250f\u2501\u2501 {label}",
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
    if pool:
        lines.append(f"\u2503  Pool        {pool}")

    lines.append("\u2503")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"\u2517\u2501\u2501 {now}")

    return "\n".join(lines)


# ── Global state ─────────────────────────────────────────────────────────────
# In-memory (resets on restart).

all_markets_latest: dict[str, dict] = {}  # address -> market dict
all_markets_previous: dict[str, dict] = {}  # previous fetch snapshot
known_market_addresses: set[str] = set()  # for new-market detection
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
    """
    global all_markets_latest, all_markets_previous, known_market_addresses, first_fetch_done

    all_markets_previous = dict(all_markets_latest)

    new_snapshot = {}
    for m in markets:
        addr = m["address"]
        new_snapshot[addr] = m
    all_markets_latest = new_snapshot

    new_markets = []
    current_addresses = set(new_snapshot.keys())
    if first_fetch_done:
        newly_found = current_addresses - known_market_addresses
        for addr in newly_found:
            new_markets.append(new_snapshot[addr])

    known_market_addresses = current_addresses
    first_fetch_done = True

    return new_markets


def _pct_change(old: float | None, new: float | None) -> float | None:
    """Calculate percentage change. Returns None if either value is missing/zero."""
    if old is None or new is None or old == 0:
        return None
    return (new - old) / abs(old)


# ── Alert detection ──────────────────────────────────────────────────────────


def _detect_price_changes() -> list[str]:
    """Detect markets where PT or YT price changed >8% since last fetch."""
    alerts = []
    for addr, current in all_markets_latest.items():
        prev = all_markets_previous.get(addr)
        if not prev:
            continue

        label = current.get("label", addr[:8])

        pt_change = _pct_change(prev.get("pt_price"), current.get("pt_price"))
        if pt_change is not None and abs(pt_change) >= PCT_CHANGE_THRESHOLD:
            direction = "\U0001f53a" if pt_change > 0 else "\U0001f53b"
            alerts.append(
                f"{direction} PT price move \u2014 {label}\n"
                f"   {prev['pt_price']:.4f} \u2192 {current['pt_price']:.4f} ({pt_change:+.1%})"
            )

        apy_change = _pct_change(prev.get("implied_apy"), current.get("implied_apy"))
        if apy_change is not None and abs(apy_change) >= PCT_CHANGE_THRESHOLD:
            direction = "\U0001f53a" if apy_change > 0 else "\U0001f53b"
            prev_apy = prev["implied_apy"] * 100
            cur_apy = current["implied_apy"] * 100
            alerts.append(
                f"{direction} Implied APY move \u2014 {label}\n"
                f"   {prev_apy:.1f}% \u2192 {cur_apy:.1f}% ({apy_change:+.1%})"
            )

    return alerts


def _detect_pool_fills() -> list[str]:
    """Detect markets where pool fill crossed above 80% (edge-triggered)."""
    alerts = []
    for addr, current in all_markets_latest.items():
        fill = current.get("pool_fill_pct")
        if fill is None or fill < POOL_FILL_THRESHOLD:
            continue

        prev = all_markets_previous.get(addr)
        prev_fill = prev.get("pool_fill_pct") if prev else None

        if prev_fill is not None and prev_fill >= POOL_FILL_THRESHOLD:
            continue

        label = current.get("label", addr[:8])
        pct = fill * 100
        alerts.append(
            f"\U0001f7e1 {label}\n"
            f"  Pool filled to {pct:.1f}%"
        )

    return alerts


def _check_user_alerts(markets_by_addr: dict[str, dict]) -> dict[int, list[str]]:
    """Check user-level percentage alerts. Returns {chat_id: [messages]}."""
    results: dict[int, list[str]] = {}

    for chat_id, alerts in user_alerts.items():
        for alert in alerts:
            if alert["fired"]:
                continue

            pct_threshold = alert["pct"]
            market_filter = alert.get("market_filter")

            for addr, current in markets_by_addr.items():
                prev = all_markets_previous.get(addr)
                if not prev:
                    continue

                label = current.get("label", addr[:8])

                if market_filter:
                    filt = market_filter.lower()
                    if filt not in label.lower() and filt not in addr.lower():
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
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning("Failed to send to %s: %s", chat_id, e)


# ── Global tick ──────────────────────────────────────────────────────────────


async def _global_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Single global job running every 60s:
    1. Fetch all markets
    2. Update state, detect new markets
    3. Fire default alerts (new market, 8% change, 80% pool)
    4. Send periodic dashboards (respecting per-user interval)
    5. Check user-level custom alerts
    """
    logger.info("Global tick starting")

    markets = fetch_all_markets()
    if not markets:
        logger.warning("Global tick: fetch returned no markets")
        return

    new_markets = _update_market_state(markets)

    # Default alert: new market
    for m in new_markets:
        label = m.get("label", m["address"][:8])
        text = (
            f"\U0001f195 New market listed!\n"
            f"{label}\n"
            f"PT Price: {m.get('pt_price', 0):.4f}"
        )
        await _broadcast(context, text)

    # Default alert: 8% price change
    price_alerts = _detect_price_changes()
    if price_alerts:
        header = "\u26a0\ufe0f Large price movements:\n\n"
        text = header + "\n\n".join(price_alerts)
        await _broadcast(context, text)

    # Default alert: 80% pool fill
    pool_alerts = _detect_pool_fills()
    if pool_alerts:
        header = "\U0001f4a7 Pool fill alerts:\n\n"
        text = header + "\n\n".join(pool_alerts)
        await _broadcast(context, text)

    # Periodic dashboards
    now = time.time()
    dashboard_text = None

    for chat_id in list(subscribed_chats.keys()):
        interval = DEFAULT_INTERVAL_SECONDS
        last_sent = last_dashboard_sent.get(chat_id, 0)

        if now - last_sent >= interval:
            if dashboard_text is None:
                dashboard_text = format_all_markets_dashboard(markets)
            try:
                await context.bot.send_message(chat_id=chat_id, text=dashboard_text)
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

    logger.info("Global tick complete — %d markets tracked", len(markets))


# ── Telegram command handlers ────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message with usage instructions."""
    msg = (
        "\U0001f4ca *ExponentFi Yield Monitor*\n\n"
        "Track PT/YT/LP prices across all Exponent markets\\.\n\n"
        "*Always\\-on alerts:*\n"
        "\u2022 New market listings\n"
        "\u2022 PT/YT price moves \\>8%\n"
        "\u2022 LP pool fills \\>80%\n\n"
        "*Commands:*\n"
        "/markets \\- All\\-markets dashboard\n"
        "/market `<filter>` \\- Detailed view \\(e\\.g\\. /market bulk\\)\n"
        "/setalert `<pct>` `[market]` \\- Custom % alert\n"
        "/alerts \\- List your alerts\n"
        "/deletealert `<id>` \\- Delete a specific alert\n"
        "/subscribe \\- Periodic dashboards \\(10 min\\)\n"
        "/unsubscribe \\- Stop dashboards\n"
    )
    await update.message.reply_text(msg, parse_mode="MarkdownV2")


async def cmd_markets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all-markets dashboard. Optional filter argument."""
    await update.message.reply_text("\u23f3 Fetching data...")

    if all_markets_latest:
        markets = list(all_markets_latest.values())
    else:
        markets = fetch_all_markets()
        if markets:
            _update_market_state(markets)

    if not markets:
        await update.message.reply_text("\u274c Failed to fetch data. Try again later.")
        return

    # Optional filter
    filt = " ".join(context.args).lower() if context.args else None
    if filt:
        markets = [
            m for m in markets
            if filt in (m.get("label") or "").lower()
            or filt in (m.get("address") or "").lower()
            or filt in (m.get("platform") or "").lower()
        ]
        if not markets:
            await update.message.reply_text(f"No markets matching '{filt}'.")
            return

    dashboard = format_all_markets_dashboard(markets)
    await update.message.reply_text(dashboard)


async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detailed view of a single market (or filtered set)."""
    if not context.args:
        await update.message.reply_text("Usage: /market <filter>\nExample: /market bulk")
        return

    filt = " ".join(context.args).lower()

    if all_markets_latest:
        markets = list(all_markets_latest.values())
    else:
        markets = fetch_all_markets()
        if markets:
            _update_market_state(markets)

    if not markets:
        await update.message.reply_text("\u274c No market data available.")
        return

    matched = [
        m for m in markets
        if filt in (m.get("label") or "").lower()
        or filt in (m.get("address") or "").lower()
        or filt in (m.get("platform") or "").lower()
    ]

    if not matched:
        await update.message.reply_text(f"No markets matching '{filt}'.")
        return

    for m in matched[:5]:
        detail = format_market_detail(m)
        await update.message.reply_text(detail)

    if len(matched) > 5:
        await update.message.reply_text(f"...and {len(matched) - 5} more. Use a more specific filter.")


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
            "Default alerts (new market, 8% price change, 80% pool fill) "
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


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Subscribe to periodic dashboards + default alerts."""
    chat_id = update.effective_chat.id
    subscribed_chats[chat_id] = True
    last_dashboard_sent[chat_id] = 0

    await update.message.reply_text(
        f"\u2705 Subscribed! Dashboards every {DEFAULT_INTERVAL_SECONDS // 60} min.\n"
        f"Default alerts (new markets, 8% moves, 80% pool fill) are now active."
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
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))

    app.job_queue.run_repeating(
        _global_tick,
        interval=TICK_INTERVAL_SECONDS,
        first=10,
        name="global_tick",
    )

    logger.info("ExponentFi Yield Monitor starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
