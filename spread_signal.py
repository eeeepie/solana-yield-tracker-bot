#!/usr/bin/env python3
"""
Cross-venue PT spread signal engine.

Matches EXP and RTX markets for the same underlying asset, tracks hourly
spread history, and emits tiered signals (INFO / WATCH / ACT) when spreads
become statistically extreme.

Designed to be imported by pt_monitor.py and called every tick.
Can also run standalone for debugging.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "universe_max_gap_days": 30,
    "lookback_hours": 336,         # 14 days
    "min_obs": 100,                # ~4 days (lower than PRD to catch opportunities earlier)
    "full_obs": 240,               # 10 days — full confidence
    "k_info_high": 1.0,            # Tier 1: gentle heads-up
    "k_watch_high": 1.5,           # Tier 2: pay attention
    "k_act_high": 2.0,             # Tier 3: actionable
    "k_info_medium": 1.2,
    "k_watch_medium": 1.7,
    "k_act_medium": 2.2,
    "jump_sigma_threshold": 1.0,
    "min_sigma_bps": 1.0,
    "cooldown_seconds": 14400,     # 4 hours
    "max_history_hours": 720,      # 30 days ring buffer
}


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class CrossVenuePair:
    pair_id: str
    underlying: str
    platform: str
    exp_market: dict
    rtx_market: dict
    maturity_gap_days: float
    maturity_class: str  # "HIGH" (≤3d) or "MEDIUM" (3-30d)


@dataclass
class SpreadStats:
    n_obs: int = 0
    mu: float = 0.0
    sigma: float = 0.0
    q05: float = 0.0
    q10: float = 0.0
    q90: float = 0.0
    q95: float = 0.0
    latest: float = 0.0
    previous: float = 0.0
    latest_ts: int = 0


@dataclass
class SignalResult:
    pair_id: str
    market_title: str
    ts: int
    # Tiered state: NO_TRIGGER, INFO, WATCH, ACT
    state: str
    reason_codes: list[str]
    # Classification
    maturity_class: str
    maturity_gap_days: float
    basis_regime: str       # STRUCTURAL or NORMAL
    # Metrics
    spread_bps: float
    mu: float
    sigma: float
    z: float
    q05: float
    q10: float
    q90: float
    q95: float
    jump_bps: float
    jump_sigma: float
    # Source data
    exp_pt: float
    rtx_pt: float
    exp_label: str
    rtx_label: str
    # Eligibility
    eligible: bool = True
    eligibility_note: str = ""


# ── Spread history store ─────────────────────────────────────────────────────

class SpreadHistory:
    """Rolling hourly spread history per market pair."""

    def __init__(self, max_hours: int = 720):
        self._max = max_hours
        # pair_id → deque of (ts_hour, pt_spread_bps)
        self._data: dict[str, deque] = defaultdict(lambda: deque(maxlen=max_hours))

    def record(self, pair_id: str, ts: int, exp_pt: float, rtx_pt: float) -> None:
        """Record a spread observation, bucketed by hour."""
        if rtx_pt <= 0 or exp_pt <= 0:
            return
        mid = (exp_pt + rtx_pt) / 2.0
        spread_bps = (exp_pt - rtx_pt) / mid * 10_000
        ts_hour = (ts // 3600) * 3600

        buf = self._data[pair_id]
        # Update existing hour bucket or append new
        if buf and buf[-1][0] == ts_hour:
            buf[-1] = (ts_hour, spread_bps)
        else:
            buf.append((ts_hour, spread_bps))

    def get_stats(self, pair_id: str, lookback_hours: int = 336) -> SpreadStats:
        """Compute rolling statistics over the lookback window."""
        buf = self._data.get(pair_id)
        if not buf or len(buf) < 2:
            return SpreadStats()

        now_hour = (int(time.time()) // 3600) * 3600
        cutoff = now_hour - lookback_hours * 3600

        values = [v for ts, v in buf if ts >= cutoff]
        if len(values) < 2:
            return SpreadStats()

        n = len(values)
        mu = sum(values) / n
        variance = sum((v - mu) ** 2 for v in values) / (n - 1) if n > 1 else 0
        sigma = math.sqrt(variance)

        sorted_vals = sorted(values)
        def percentile(p):
            k = (n - 1) * p
            f = int(k)
            c = min(f + 1, n - 1)
            d = k - f
            return sorted_vals[f] + d * (sorted_vals[c] - sorted_vals[f])

        latest = values[-1]
        previous = values[-2] if n >= 2 else latest

        return SpreadStats(
            n_obs=n,
            mu=mu,
            sigma=sigma,
            q05=percentile(0.05),
            q10=percentile(0.10),
            q90=percentile(0.90),
            q95=percentile(0.95),
            latest=latest,
            previous=previous,
            latest_ts=buf[-1][0],
        )

    def pair_ids(self) -> list[str]:
        return list(self._data.keys())

    def obs_count(self, pair_id: str) -> int:
        return len(self._data.get(pair_id, []))


# ── Pair matching ────────────────────────────────────────────────────────────

def _normalize_symbol(m: dict) -> str:
    """Normalize underlying symbol for cross-venue matching."""
    sym = (m.get("underlying_symbol") or "").strip()
    # Normalize case variations
    return sym.lower()


def match_cross_venue_pairs(
    markets: list[dict],
    max_gap_days: float = 30,
) -> list[CrossVenuePair]:
    """Match EXP and RTX markets by underlying symbol + maturity proximity."""
    exp_by_sym: dict[str, list[dict]] = defaultdict(list)
    rtx_by_sym: dict[str, list[dict]] = defaultdict(list)

    for m in markets:
        source = (m.get("source") or "").lower()
        sym = _normalize_symbol(m)
        if not sym or sym == "?" or m.get("pt_price") is None:
            continue
        if source == "exponent":
            exp_by_sym[sym].append(m)
        elif source == "ratex":
            rtx_by_sym[sym].append(m)

    pairs = []
    for sym in set(exp_by_sym) & set(rtx_by_sym):
        for exp_m in exp_by_sym[sym]:
            exp_mat = exp_m.get("maturity_ts")
            if not exp_mat:
                continue

            # Find closest RTX maturity
            best_rtx = None
            best_gap = float("inf")
            for rtx_m in rtx_by_sym[sym]:
                rtx_mat = rtx_m.get("maturity_ts")
                if not rtx_mat:
                    continue
                gap = abs(exp_mat - rtx_mat) / 86400.0
                if gap < best_gap:
                    best_gap = gap
                    best_rtx = rtx_m

            if best_rtx is None or best_gap > max_gap_days:
                continue

            mat_class = "HIGH" if best_gap <= 3 else "MEDIUM"
            platform = exp_m.get("platform") or "?"
            underlying = exp_m.get("underlying_symbol") or sym

            pair_id = f"{underlying}_{platform}"

            pairs.append(CrossVenuePair(
                pair_id=pair_id,
                underlying=underlying,
                platform=platform,
                exp_market=exp_m,
                rtx_market=best_rtx,
                maturity_gap_days=best_gap,
                maturity_class=mat_class,
            ))

    return pairs


# ── Signal computation ───────────────────────────────────────────────────────

def compute_signal(
    pair: CrossVenuePair,
    stats: SpreadStats,
    config: dict,
) -> SignalResult:
    """Compute tiered signal for a single market pair."""

    exp_pt = pair.exp_market.get("pt_price", 0)
    rtx_pt = pair.rtx_market.get("pt_price", 0)
    exp_label = pair.exp_market.get("label", "?")
    rtx_label = pair.rtx_market.get("label", "?")

    now_ts = int(time.time())

    base = dict(
        pair_id=pair.pair_id,
        market_title=f"{pair.underlying} ({pair.platform})",
        ts=now_ts,
        maturity_class=pair.maturity_class,
        maturity_gap_days=pair.maturity_gap_days,
        exp_pt=exp_pt,
        rtx_pt=rtx_pt,
        exp_label=exp_label,
        rtx_label=rtx_label,
    )

    # Eligibility checks
    min_obs = config.get("min_obs", 100)
    min_sigma = config.get("min_sigma_bps", 1.0)

    if stats.n_obs < min_obs:
        return SignalResult(
            **base, state="NO_TRIGGER", reason_codes=[],
            basis_regime="UNKNOWN", spread_bps=stats.latest,
            mu=stats.mu, sigma=stats.sigma, z=0, q05=0, q10=0, q90=0, q95=0,
            jump_bps=0, jump_sigma=0,
            eligible=False, eligibility_note=f"INSUFFICIENT_DATA ({stats.n_obs}/{min_obs})",
        )

    if stats.sigma < min_sigma:
        return SignalResult(
            **base, state="NO_TRIGGER", reason_codes=[],
            basis_regime="UNKNOWN", spread_bps=stats.latest,
            mu=stats.mu, sigma=stats.sigma, z=0, q05=0, q10=0, q90=0, q95=0,
            jump_bps=0, jump_sigma=0,
            eligible=False, eligibility_note="INSUFFICIENT_VARIANCE",
        )

    # Staleness check (>2h since last observation)
    if stats.latest_ts > 0 and (now_ts - stats.latest_ts) > 7200:
        return SignalResult(
            **base, state="NO_TRIGGER", reason_codes=[],
            basis_regime="UNKNOWN", spread_bps=stats.latest,
            mu=stats.mu, sigma=stats.sigma, z=0, q05=0, q10=0, q90=0, q95=0,
            jump_bps=0, jump_sigma=0,
            eligible=False, eligibility_note="STALE_DATA",
        )

    # Core metrics
    x = stats.latest
    z = (x - stats.mu) / stats.sigma
    jump = x - stats.previous
    jump_sigma = jump / stats.sigma

    # Suppress jump if gap between latest and previous is > 2 hours
    # (e.g. after bootstrap gap or data outage)
    buf = _spread_history._data.get(pair.pair_id)
    if buf and len(buf) >= 2:
        latest_ts = buf[-1][0]
        prev_ts = buf[-2][0]
        if latest_ts - prev_ts > 7200:
            jump = 0.0
            jump_sigma = 0.0

    # Basis regime
    basis_regime = "STRUCTURAL" if abs(stats.mu) > 3 * stats.sigma else "NORMAL"

    # Thresholds by maturity class
    if pair.maturity_class == "HIGH":
        k_info = config.get("k_info_high", 1.0)
        k_watch = config.get("k_watch_high", 1.5)
        k_act = config.get("k_act_high", 2.0)
    else:
        k_info = config.get("k_info_medium", 1.2)
        k_watch = config.get("k_watch_medium", 1.7)
        k_act = config.get("k_act_medium", 2.2)

    jump_thresh = config.get("jump_sigma_threshold", 1.0)

    # Flags
    z_info = abs(z) >= k_info
    z_watch = abs(z) >= k_watch
    z_act = abs(z) >= k_act

    tail_watch = x <= stats.q10 or x >= stats.q90
    tail_act = x <= stats.q05 or x >= stats.q95

    jump_hit = abs(jump_sigma) >= jump_thresh

    # Tiered decision logic
    state = "NO_TRIGGER"
    reasons = []

    # ACT: z extreme OR (tail extreme + jump)
    if z_act:
        state = "ACT"
        reasons.append("Z_ACT")
    elif tail_act and jump_hit:
        state = "ACT"
        reasons.append("TAIL_ACT")
        reasons.append("JUMP")

    # WATCH: z moderate OR tail moderate OR jump alone
    if state == "NO_TRIGGER":
        if z_watch:
            state = "WATCH"
            reasons.append("Z_WATCH")
        elif tail_watch and jump_hit:
            state = "WATCH"
            reasons.append("TAIL_WATCH")
            reasons.append("JUMP")
        elif jump_hit:
            state = "WATCH"
            reasons.append("JUMP")

    # INFO: z mild OR any tail
    if state == "NO_TRIGGER":
        if z_info:
            state = "INFO"
            reasons.append("Z_INFO")
        elif tail_watch:
            state = "INFO"
            reasons.append("TAIL")

    # Confidence downgrade for early data
    full_obs = config.get("full_obs", 240)
    if stats.n_obs < full_obs and state == "ACT":
        state = "WATCH"
        reasons.append("LOW_DATA_DOWNGRADE")

    return SignalResult(
        **base,
        state=state,
        reason_codes=reasons,
        basis_regime=basis_regime,
        spread_bps=x,
        mu=stats.mu,
        sigma=stats.sigma,
        z=z,
        q05=stats.q05,
        q10=stats.q10,
        q90=stats.q90,
        q95=stats.q95,
        jump_bps=jump,
        jump_sigma=jump_sigma,
    )


# ── Cooldown tracker ─────────────────────────────────────────────────────────

class CooldownTracker:
    """Prevent duplicate alerts within cooldown window."""

    def __init__(self):
        # pair_id → (last_state, last_alert_ts)
        self._last: dict[str, tuple[str, float]] = {}

    def should_alert(self, pair_id: str, state: str, cooldown_seconds: int) -> bool:
        """Return True if this signal should produce an alert."""
        if state == "NO_TRIGGER":
            # Clear cooldown when state drops
            self._last.pop(pair_id, None)
            return False

        now = time.time()
        prev = self._last.get(pair_id)

        if prev is None:
            # First signal for this pair
            self._last[pair_id] = (state, now)
            return True

        prev_state, prev_ts = prev
        elapsed = now - prev_ts

        # Always re-alert on escalation
        state_rank = {"INFO": 1, "WATCH": 2, "ACT": 3}
        if state_rank.get(state, 0) > state_rank.get(prev_state, 0):
            self._last[pair_id] = (state, now)
            return True

        # Respect cooldown for same/lower state
        if elapsed >= cooldown_seconds:
            self._last[pair_id] = (state, now)
            return True

        return False


# ── Bootstrap from historical CSV ────────────────────────────────────────────

# Map from comprehensive CSV market_slug → pair_id used in live matching
_SLUG_TO_PAIR = {
    "xsol_apr": "xSOL_hylo",
    "hyusd_apr": "hyUSD_hylo",
    "hylosol_apr": "hyloSOL_hylo",
    "onre": "ONyc_onre",
    # fragbtc and fragsol excluded (gap > 30d)
}


def bootstrap_from_csv(history: SpreadHistory, csv_path: str) -> int:
    """Load historical hourly spread data from comprehensive analysis CSV."""
    import csv
    import os

    if not os.path.exists(csv_path):
        logger.warning("Bootstrap CSV not found: %s", csv_path)
        return 0

    loaded = 0
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            slug = row.get("market_slug", "")
            pair_id = _SLUG_TO_PAIR.get(slug)
            if not pair_id:
                continue

            try:
                ts_ms = int(float(row["timestamp_hour_ms"]))
                ts = ts_ms // 1000
                spread_bps = float(row["pt_diff_bps_clean"])
            except (KeyError, ValueError, TypeError):
                continue

            ts_hour = (ts // 3600) * 3600
            buf = history._data[pair_id]
            if not buf or buf[-1][0] != ts_hour:
                buf.append((ts_hour, spread_bps))
                loaded += 1

    logger.info("Bootstrapped %d hourly observations from %s", loaded, csv_path)
    return loaded


# ── Main entry point ─────────────────────────────────────────────────────────

# Module-level singletons (persisted across bot ticks)
_spread_history = SpreadHistory()
_cooldown = CooldownTracker()
_bootstrapped = False


def evaluate_signals(
    markets: list[dict],
    config: dict | None = None,
) -> list[SignalResult]:
    """
    Main entry point. Called from bot tick with current market data.

    1. Match cross-venue pairs
    2. Record spread observations
    3. Compute signals
    4. Return all results (caller decides which to alert on)
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}

    global _bootstrapped
    if not _bootstrapped:
        csv_path = "analyze/output/comprehensive_20260301_145659/all_markets_cleaned_combined.csv"
        bootstrap_from_csv(_spread_history, csv_path)
        _bootstrapped = True

    pairs = match_cross_venue_pairs(markets, max_gap_days=cfg["universe_max_gap_days"])

    now_ts = int(time.time())
    results = []

    for pair in pairs:
        exp_pt = pair.exp_market.get("pt_price")
        rtx_pt = pair.rtx_market.get("pt_price")

        if exp_pt is not None and rtx_pt is not None:
            _spread_history.record(pair.pair_id, now_ts, exp_pt, rtx_pt)

        stats = _spread_history.get_stats(pair.pair_id, cfg["lookback_hours"])
        signal = compute_signal(pair, stats, cfg)
        results.append(signal)

    return results


def get_alertable_signals(
    signals: list[SignalResult],
    config: dict | None = None,
) -> list[SignalResult]:
    """Filter signals to only those that should produce alerts (respecting cooldown)."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    cooldown_s = cfg.get("cooldown_seconds", 14400)

    alertable = []
    for s in signals:
        if not s.eligible:
            continue
        if s.state == "NO_TRIGGER":
            _cooldown.should_alert(s.pair_id, s.state, cooldown_s)  # reset cooldown
            continue
        if _cooldown.should_alert(s.pair_id, s.state, cooldown_s):
            alertable.append(s)

    return alertable


# ── Telegram formatting ──────────────────────────────────────────────────────

_STATE_EMOJI = {
    "ACT": "\U0001f534",    # red circle
    "WATCH": "\U0001f7e1",  # yellow circle
    "INFO": "\U0001f535",   # blue circle
}


def format_signal_alert(s: SignalResult) -> str:
    """Format a signal result as a Telegram message."""
    emoji = _STATE_EMOJI.get(s.state, "")
    direction = "EXP richer" if s.spread_bps > 0 else "RTX richer"

    lines = [
        f"{emoji} {s.state} — {s.market_title}",
        f"  EXP: {s.exp_pt:.6f} ({s.exp_label})",
        f"  RTX: {s.rtx_pt:.6f} ({s.rtx_label})",
        f"  Spread: {s.spread_bps:+.1f} bps (z={s.z:+.2f}) {direction}",
    ]

    if abs(s.jump_bps) > 0.5:
        lines.append(f"  Jump: {s.jump_bps:+.1f} bps ({s.jump_sigma:+.1f}\u03c3) last hour")

    class_str = f"{s.maturity_class} ({s.maturity_gap_days:.1f}d gap)"
    lines.append(f"  Class: {class_str} \u00b7 Basis: {s.basis_regime}")
    lines.append(f"  Reason: {', '.join(s.reason_codes)}")

    if not s.eligible:
        lines.append(f"  \u26a0 {s.eligibility_note}")

    return "\n".join(lines)


def format_signal_summary(signals: list[SignalResult]) -> str:
    """Format all signals as a compact status summary."""
    if not signals:
        return "No cross-venue pairs found."

    lines = ["\U0001f4e1 Cross-Venue Spread Monitor"]
    lines.append(f"  {len(signals)} pairs tracked\n")

    for s in sorted(signals, key=lambda x: {"ACT": 0, "WATCH": 1, "INFO": 2, "NO_TRIGGER": 3}.get(x.state, 4)):
        emoji = _STATE_EMOJI.get(s.state, "\u26aa")
        eligible_tag = "" if s.eligible else f" [{s.eligibility_note}]"
        spread_str = f"{s.spread_bps:+.1f} bps" if s.eligible else "n/a"
        z_str = f"z={s.z:+.1f}" if s.eligible and s.sigma > 0 else ""
        obs_str = f"({s.mu:.0f}\u00b1{s.sigma:.0f} bps, n={_spread_history.obs_count(s.pair_id)})"

        lines.append(f"{emoji} {s.market_title}: {spread_str} {z_str} {obs_str}{eligible_tag}")

    return "\n".join(lines)


# ── Standalone runner ────────────────────────────────────────────────────────

def main() -> None:
    """Run standalone: fetch markets, compute signals, print results."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    # Import scrapers
    import sys
    sys.path.insert(0, ".")
    from pt_monitor import fetch_all_markets
    from ratex_scraper import fetch_ratex_markets

    exp = fetch_all_markets()
    sol_price = next((m.get("sol_price_usd") for m in exp if m.get("sol_price_usd")), None)
    rtx = fetch_ratex_markets(sol_price_usd=sol_price)
    markets = exp + rtx

    print(f"\nFetched {len(exp)} EXP + {len(rtx)} RTX markets\n")

    # Show matched pairs
    pairs = match_cross_venue_pairs(markets)
    print(f"Matched {len(pairs)} cross-venue pairs:")
    for p in pairs:
        exp_pt = p.exp_market.get("pt_price", 0)
        rtx_pt = p.rtx_market.get("pt_price", 0)
        mid = (exp_pt + rtx_pt) / 2 if (exp_pt + rtx_pt) > 0 else 1
        spread = (exp_pt - rtx_pt) / mid * 10_000
        print(f"  {p.pair_id:25s} gap={p.maturity_gap_days:.1f}d [{p.maturity_class}]  "
              f"EXP={exp_pt:.6f} RTX={rtx_pt:.6f} spread={spread:+.1f} bps")

    # Record one observation and compute signals
    signals = evaluate_signals(markets)
    print(f"\n{format_signal_summary(signals)}")

    # Show any that would alert (won't fire with only 1 observation)
    alertable = get_alertable_signals(signals)
    if alertable:
        print(f"\nAlertable signals:")
        for s in alertable:
            print(format_signal_alert(s))
    else:
        print(f"\nNo alertable signals (need {DEFAULT_CONFIG['min_obs']}+ hourly observations to start)")


if __name__ == "__main__":
    main()
