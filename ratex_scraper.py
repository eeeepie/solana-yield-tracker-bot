#!/usr/bin/env python3
"""
Rate-X market scraper.

Uses the same backend RPC methods as the Rate-X frontend:
- AdminSvr.querySymbol  → categories + symbols
- Trade{LEVEL1}Svr.dc.trade.dprice  → oracle exchange rates
- MDSvr.queryTrade  → live YT prices, yields, liquidity

Field name conventions (from API inspection):
- categories/symbols: snake_case (symbol_category, due_date_l, etc.)
- dprice: snake_case (security_id, rate_price, symbol_category)
- queryTrade: CamelCase (SecurityID, LastPrice, Yield, IndexPrice, etc.)
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import random
import re
import string
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

RATEX_API_URL = os.getenv("RATEX_API_URL", "https://api.rate-x.io")
RATEX_TIMEOUT = float(os.getenv("RATEX_TIMEOUT", "25"))

# Level-1 categories whose trade_currency is USD-pegged.
_STABLE_LEVEL1 = {
    "USDE", "USDEV2", "SUSDE", "SUSDEV2", "SUSD", "SUSDU",
    "HYUSD", "SHYUSD", "ONYC", "JLUSDC", "KLEND", "MUSD10XSOL",
    "USDSTAR", "USDSTARV2", "USDSTARV3", "YU",
}

# Map level2 symbol_category → human-friendly protocol name for the label.
# Verified against each project's docs / announcements.
_PROTOCOL_MAP = {
    # SOL LSTs
    "jitosol": "jito",
    "bbsol": "bybit",
    "bnsol": "binance",
    "jsol": "jpool",
    # Sonic SVM
    "ssol": "sonic",
    "slsol": "sonic",
    # Jupiter
    "jlp": "jupiter",
    "jlusdc": "jupiter",
    # Flash Trade
    "flp": "flash",
    # Hylo (hyUSD, sHYUSD, hyloSOL, hyloSOL+, xSOL are all Hylo)
    "hylosol": "hylo",
    "hylosol+": "hylo",
    "hyusd": "hylo",
    "shyusd": "hylo",
    "xsol": "hylo",
    # OnRe (reinsurance yield)
    "onyc": "onre",
    # Fragmetric (liquid restaking)
    "fragsol": "fragmetric",
    "fragbtc": "fragmetric",
    "fragjto": "fragmetric",
    # Adrastea (liquid restaking — lrtsSOL and adraSOL)
    "lrtssol": "adrastea",
    "adrasol": "adrastea",
    # Ethena (USDe, sUSDe)
    "usde": "ethena",
    "susde": "ethena",
    # Ethena (sUSD — Ethena's sUSD on Solana)
    "susd": "ethena",
    # Unitas (sUSDu)
    "susdu": "unitas",
    # Perena (USD*)
    "usd*": "perena",
    # Huma Finance (PST = PayFi Strategy Token)
    "pst": "huma",
    # Kamino/Klend
    "kusdc": "kamino",
    "kljlpusdc": "kamino",
    # Kyros (liquid restaking)
    "kysol": "kyros",
    "kyjto": "kyros",
    # Renzo (liquid restaking)
    "ezsol": "renzo",
    # DeFi Dev Corp
    "dfdvsol": "defi dev",
    # Mooncake (mUSD 10xSOL leveraged market)
    "musd10xsol": "mooncake",
    "musd(10xsol)": "mooncake",
    # Yala (YU stablecoin)
    "yu": "yala",
}


def _build_cid() -> str:
    return "-".join(
        "".join(random.choice(string.hexdigits.lower()[:16]) for _ in range(4))
        for _ in range(12)
    )


def _rpc_call(server_name: str, method: str, content: dict | None = None) -> dict | list:
    payload = {
        "serverName": server_name,
        "method": method,
        "content": {"cid": _build_cid(), **(content or {})},
    }
    resp = requests.post(
        RATEX_API_URL,
        json=payload,
        timeout=RATEX_TIMEOUT,
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()

    body = resp.json()
    if isinstance(body, dict):
        code = body.get("code")
        if code not in (None, 0):
            msg = body.get("msg") or body.get("message") or "unknown error"
            raise RuntimeError(f"Rate-X RPC failed: {method} code={code} msg={msg}")
        return body.get("data") if "data" in body else body

    return body


def _parse_maturity_suffix(security_id: str) -> int | None:
    """Parse YYMM suffix from security_id like 'JitoSOL-2506' → epoch ts."""
    if "-" not in security_id:
        return None
    suffix = security_id.rsplit("-", 1)[1]
    m = re.fullmatch(r"(\d{2})(\d{2})", suffix)
    if not m:
        return None
    yy, mm = int(m.group(1)), int(m.group(2))
    year = 2000 + yy
    if not (1 <= mm <= 12):
        return None
    last_day = calendar.monthrange(year, mm)[1]
    dt = datetime(year, mm, last_day, 23, 59, 59, tzinfo=timezone.utc)
    return int(dt.timestamp())


def _resolve_protocol(level2: str) -> str:
    """Map level2 category to a human-friendly protocol name."""
    key = level2.strip().lower()
    if key in _PROTOCOL_MAP:
        return _PROTOCOL_MAP[key]
    # Strip leading 'w' prefix and trailing version numbers: "wfragBTCV2" → "fragbtc"
    stripped = re.sub(r"^w", "", key)
    stripped = re.sub(r"v\d+$", "", stripped)
    if stripped in _PROTOCOL_MAP:
        return _PROTOCOL_MAP[stripped]
    return key


def _format_label(protocol: str, asset: str, maturity_ts: int | None) -> str:
    if maturity_ts:
        dt = datetime.fromtimestamp(maturity_ts, tz=timezone.utc)
        date_str = dt.strftime("%d%b%y").lstrip("0")
        return f"{protocol} · {asset} · {date_str}"
    return f"{protocol} · {asset}"


def _is_sol_based(level1: str) -> bool:
    """Check if a level-1 category is SOL-denominated."""
    upper = level1.upper()
    return upper == "SOL" or upper.endswith("SOL") or upper.endswith("SOLV2")


def _is_stable_based(level1: str) -> bool:
    return level1.upper() in _STABLE_LEVEL1


def _fetch_base_prices_usd() -> dict[str, float]:
    """Get SOL, BTC, JTO USD prices from CoinGecko."""
    prices: dict[str, float] = {}
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana,bitcoin,jito-governance-token", "vs_currencies": "usd"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if "solana" in data:
            prices["SOL"] = data["solana"]["usd"]
        if "bitcoin" in data:
            prices["BTC"] = data["bitcoin"]["usd"]
        if "jito-governance-token" in data:
            prices["JTO"] = data["jito-governance-token"]["usd"]
    except Exception as exc:
        logger.warning("Failed to fetch prices from CoinGecko: %s", exc)
    return prices


def _detect_base_token(level1: str, trade_currency: str) -> str:
    """Detect which base token a market is denominated in: SOL, BTC, JTO, or USD."""
    l1 = level1.upper()
    tc = trade_currency.upper()

    # BTC-based
    if "BTC" in l1 or "BTC" in tc:
        return "BTC"
    # JTO-based
    if "JTO" in l1 or "JTO" in tc:
        return "JTO"
    # Stablecoin
    if l1 in _STABLE_LEVEL1:
        return "USD"
    # Explicitly SOL-denominated (trade_currency is "SOL")
    if tc == "SOL":
        return "SOL"
    # Level1 contains "SOL" (HYLOSOL, FRAGSOL, XSOL, etc.) — oracle rate is asset/SOL
    if "SOL" in l1:
        return "SOL"
    # JLP, FLP, PST, etc. — self-referencing tokens. Oracle rate is cumulative yield index.
    # For these, base_asset_usd = token_USD_price but we don't have it easily.
    # Best effort: these are SOL-ecosystem DeFi tokens, approximate with SOL.
    return "SOL"


def fetch_ratex_markets(sol_price_usd: float | None = None) -> list[dict]:
    """Fetch active Rate-X markets and normalize to pt_monitor schema."""

    # 1) Get catalog: categories + symbols
    try:
        catalog = _rpc_call("AdminSvr", "querySymbol")
    except Exception as exc:
        logger.error("Rate-X querySymbol failed: %s", exc)
        return []

    if not isinstance(catalog, dict):
        logger.error("Rate-X querySymbol returned unexpected payload")
        return []

    categories = [c for c in (catalog.get("categories") or []) if isinstance(c, dict)]
    symbols = [s for s in (catalog.get("symbols") or []) if isinstance(s, dict)]

    # Build category lookup: symbol_category → category dict
    cat_by_name: dict[str, dict] = {}
    for c in categories:
        key = c.get("symbol_category")
        if key:
            cat_by_name[key] = c

    # Identify level-1 categories for dprice calls
    level1_cats = sorted({
        c["symbol_category"] for c in categories
        if c.get("level") == 1 and c.get("symbol_category")
    })

    # 2) Fetch oracle rates (dprice) per level-1 category
    rate_by_security: dict[str, dict] = {}
    for cat in level1_cats:
        try:
            rates = _rpc_call(f"Trade{cat}Svr", "dc.trade.dprice")
        except Exception:
            continue
        if not isinstance(rates, list):
            continue
        for item in rates:
            if isinstance(item, dict) and item.get("security_id"):
                rate_by_security[item["security_id"]] = item

    # 3) Fetch live trade data
    trade_by_id: dict[str, dict] = {}
    try:
        snapshot = _rpc_call("MDSvr", "queryTrade")
        if isinstance(snapshot, list):
            for item in snapshot:
                if isinstance(item, dict) and item.get("SecurityID"):
                    trade_by_id[item["SecurityID"]] = item
    except Exception as exc:
        logger.warning("Rate-X queryTrade failed: %s", exc)

    # 4) Get base token prices
    base_prices = _fetch_base_prices_usd()
    if sol_price_usd is None:
        sol_price_usd = base_prices.get("SOL")
    else:
        base_prices["SOL"] = sol_price_usd

    now_ts = int(time.time())
    markets: list[dict] = []

    for s in symbols:
        security_id = s.get("symbol") or s.get("symbol_name")
        if not security_id:
            continue

        # Parse maturity: prefer due_date_l, fallback to suffix
        maturity_ts = None
        raw_ts = s.get("due_date_l")
        if raw_ts:
            try:
                ts_val = float(raw_ts)
                # API returns milliseconds
                maturity_ts = int(ts_val / 1000) if ts_val > 1_000_000_000_000 else int(ts_val)
            except (TypeError, ValueError):
                pass
        if maturity_ts is None:
            maturity_ts = _parse_maturity_suffix(security_id)

        # Skip expired
        if maturity_ts is not None and maturity_ts < now_ts:
            continue

        level1 = (s.get("symbol_level1_category") or "").strip()
        level2 = (s.get("symbol_level2_category") or "").strip()

        # Resolve display info from level-2 category
        cat = cat_by_name.get(level2, {})
        underlying_symbol = cat.get("alias") or level2 or security_id.rsplit("-", 1)[0]
        trade_currency = cat.get("trade_currency") or level1
        protocol = _resolve_protocol(level2 or level1)

        # Get trade data (CamelCase fields)
        trade = trade_by_id.get(security_id)
        # Get dprice data (snake_case fields)
        rate_item = rate_by_security.get(security_id)

        # PT price: LastPrice is YT, PT = 1 - YT
        pt_price = None
        yt_price = None
        if trade and trade.get("LastPrice"):
            try:
                yt_price = float(trade["LastPrice"])
                if 0 <= yt_price <= 1:
                    pt_price = 1.0 - yt_price
            except (TypeError, ValueError):
                pass

        # Implied yield from trade data
        implied_apy = None
        if trade and trade.get("Yield"):
            try:
                implied_apy = float(trade["Yield"])
            except (TypeError, ValueError):
                pass

        # Oracle exchange rate (e.g. JitoSOL/SOL = 1.165)
        oracle_rate = None
        if trade and trade.get("IndexPrice"):
            try:
                oracle_rate = float(trade["IndexPrice"])
            except (TypeError, ValueError):
                pass
        # Fallback to dprice rate_price
        if oracle_rate is None and rate_item and rate_item.get("rate_price"):
            try:
                oracle_rate = float(rate_item["rate_price"])
            except (TypeError, ValueError):
                pass
        if oracle_rate is None:
            oracle_rate = 1.0

        # USD pricing: oracle_rate converts asset→base_token, then multiply by base_token USD price
        base_token = _detect_base_token(level1, trade_currency)
        base_asset_usd = None
        if base_token == "USD":
            base_asset_usd = oracle_rate  # oracle_rate ≈ 1.0 for stables
        else:
            token_price = base_prices.get(base_token)
            if token_price:
                base_asset_usd = oracle_rate * token_price

        pt_price_usd = pt_price * base_asset_usd if pt_price is not None and base_asset_usd is not None else None

        # If no trade data, try to derive PT from APY + maturity
        if pt_price is None and implied_apy is not None and maturity_ts:
            days_to_maturity = max((maturity_ts - now_ts) / 86400.0, 0.0)
            if days_to_maturity > 0:
                denom = 1 + implied_apy * (days_to_maturity / 365.0)
                if denom > 0:
                    pt_price = max(0.0, min(2.0, 1.0 / denom))
                    pt_price_usd = pt_price * base_asset_usd if base_asset_usd is not None else None

        # Compute implied APY from PT price if we have PT but no yield data
        if implied_apy is None and pt_price is not None and pt_price > 0 and maturity_ts:
            days_to_maturity = max((maturity_ts - now_ts) / 86400.0, 0.0)
            if days_to_maturity > 0:
                implied_apy = (1 / pt_price - 1) * (365.0 / days_to_maturity)

        # Liquidity (in base asset terms)
        tvl_raw = None
        if trade and trade.get("AvaLiquidity"):
            try:
                tvl_raw = float(trade["AvaLiquidity"])
            except (TypeError, ValueError):
                pass
        tvl_usd = tvl_raw * base_asset_usd if tvl_raw is not None and base_asset_usd is not None else None

        label = _format_label(protocol, underlying_symbol, maturity_ts)

        markets.append({
            "source": "ratex",
            "address": security_id,
            "platform": protocol,
            "maturity_ts": maturity_ts,
            "expired": False,
            "underlying_symbol": underlying_symbol,
            "base_asset_symbol": underlying_symbol,
            "pt_price": pt_price,
            "pt_price_usd": pt_price_usd,
            "base_asset_usd": base_asset_usd,
            "implied_apy": implied_apy,
            "pt_implied_apy": implied_apy,
            "underlying_yield": None,
            "lp_price": None,
            "lp_fees_apy": None,
            "tvl_usd": tvl_usd,
            "pool_fill_pct": None,
            "pool_cap_usd": None,
            "label": label,
            "sol_price_usd": sol_price_usd,
        })

    markets.sort(key=lambda m: (m.get("platform") or "", m.get("maturity_ts") or 0, m.get("address") or ""))
    missing_pt = sum(1 for m in markets if m.get("pt_price") is None)
    logger.info("Rate-X parsed %d active markets (missing PT=%d)", len(markets), missing_pt)
    return markets


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    markets = fetch_ratex_markets()

    # Pretty print summary
    for m in markets:
        pt = m.get("pt_price")
        apy = m.get("implied_apy")
        tvl = m.get("tvl_usd")
        pt_str = f"{pt:.6f}" if pt is not None else "—"
        apy_str = f"{apy * 100:.1f}%" if apy is not None else "—"
        tvl_str = f"${tvl:,.0f}" if tvl is not None else "—"
        print(f"{m['label']:45s}  PT={pt_str:>10s}  APY={apy_str:>8s}  TVL={tvl_str}")

    print(f"\nTotal: {len(markets)} markets")


if __name__ == "__main__":
    main()
