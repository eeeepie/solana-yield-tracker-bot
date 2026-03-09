# Solana Yield Monitor

Telegram bot that tracks yield trading markets across [Exponent Finance](https://www.exponent.finance) and [Rate-X](https://app.rate-x.io) in real-time — PT/YT prices, implied APY, pool liquidity, cross-venue spread signals, and daily yield reports.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)

## Features

- **Multi-venue dashboard** — all active markets from Exponent `[EXP]` and Rate-X `[RTX]`, grouped by protocol
- **Single market detail** — PT/YT prices, implied APY, underlying yield, pool stats / TVL
- **Always-on alerts**:
  - New market listings
  - PT/YT price moves >20%
  - LP pool fill crossing 80%
- **Cross-venue spread signals** — detects PT mispricing between EXP and RTX with tiered alerts:
  - `ACT` — statistically extreme spread (z >= 2.0), likely actionable
  - `WATCH` — moderately extreme or jump detected
  - `INFO` — mild dislocation, heads-up only
- **Daily yield report** — auto-generated at 10am UTC+8 (or on-demand via `/report`): top APY, pool fills, TVL leaders, spread status, expiring markets
- **Custom alerts** — set your own percentage thresholds, optionally filtered by market
- **Periodic updates** — subscribe to receive the dashboard every 10 minutes

## How It Works

### Exponent Finance
No public API. The bot scrapes dehydrated React Server Component state embedded in the HTML — a single page load returns data for all markets. Prices, APYs, and liquidity are extracted via regex parsing and cross-referenced against the on-page token map for USD conversion.

### Rate-X
Uses the same backend RPC that the Rate-X frontend calls:
- `AdminSvr.querySymbol` — market catalog (categories + symbols)
- `Trade{CATEGORY}Svr.dc.trade.dprice` — oracle exchange rates
- `MDSvr.queryTrade` — live YT prices, yields, liquidity

PT price is derived as `1 - LastPrice` (LastPrice is the YT price).

### Cross-Venue Spread Signals
For markets listed on both venues, the bot computes PT spread in basis points, tracks hourly history, and fires tiered alerts using z-scores, percentile tails, and jump detection. Maturity gap between venues is accounted for with stricter thresholds for mismatched maturities.

## Commands

| Command | Description |
|---------|-------------|
| `/report` | Generate daily yield report on demand |
| `/markets` | Show all active markets dashboard |
| `/market <filter>` | Detailed view of a specific market (e.g. `/market hylo`) |
| `/setalert <pct> [market]` | Alert on % change (e.g. `/setalert 5 bulk`) |
| `/alerts` | List your active alerts |
| `/deletealert <id>` | Remove an alert |
| `/subscribe` | Start receiving periodic dashboards + daily report |
| `/unsubscribe` | Stop periodic dashboards |

## Dashboard Preview

```
📊 Solana Yield Monitor
   26 active markets

━━━ FRAGMETRIC ━━━
▸ fragmetric · fragBTC · 12May26 [EXP]
   PT 0.9935 ($66592.40) · 3.3% apy
   YT 0.0065 ($432.63) · 2.4% yield
   Pool ░░░░░░░░ 12% — $40K/$339K
▸ fragmetric · fragBTC · 29Jun26 [RTX]
   PT 0.9903 · 3.0% apy
   YT 0.0097
   TVL $121

━━━ HYLO ━━━
▸ hylo · xSOL · 27Apr26 [EXP]
   PT 0.9738 ($0.08) · 17.2% apy
   YT 0.0262 ($0.00)
   Pool ▓▓▓▓▓░░░ 63% — $394K/$623K
▸ hylo · xSOL · 29Apr26 [RTX]
   PT 0.9743 ($84.11) · 17.5% apy
   YT 0.0257 ($2.22)
   TVL $328.2M
...
```

## Spread Signal Preview

```
📡 Cross-venue spread alerts:

🟡 WATCH — xSOL (hylo)
  EXP: 0.983854 (hylo · xSOL · 27Apr26)
  RTX: 0.976861 (hylo · xSOL · 29Apr26)
  Spread: +71.3 bps (z=+3.46) EXP richer
  Class: HIGH (1.5d gap) · Basis: NORMAL
  Reason: Z_ACT, LOW_DATA_DOWNGRADE

🔵 INFO — hyUSD (hylo)
  EXP: 0.991098 (hylo · hyUSD · 14Apr26)
  RTX: 0.988471 (hylo · hyUSD · 29Apr26)
  Spread: +26.5 bps (z=-1.35) EXP richer
  Class: MEDIUM (14.5d gap) · Basis: STRUCTURAL
  Reason: Z_INFO
```

## Daily Report Preview

```
📈 Solana Yield — Daily Report
🕒 2026-03-06 02:00 UTC

🔥 Top APY Markets
  17.5%  hylo · xSOL · 29Apr26 [RTX]  PT 0.9743
  17.2%  hylo · xSOL · 27Apr26 [EXP]  PT 0.9738
  12.1%  hylo · hyloSOL · 14Apr26 [EXP]  PT 0.9820
  ...

💧 Pools Above 60% Fill
    63%  hylo · xSOL · 27Apr26 [EXP] — $394K/$623K

💰 TVL Leaders
  $328.2M  hylo · xSOL · 29Apr26 [RTX]
    $1.2M  hylo · hyUSD · 29Apr26 [RTX]
  ...

📡 Cross-Venue Spreads
  🟡 WATCH xSOL (hylo): +71.3 bps (z=+3.5) EXP richer
  🔵 INFO hyUSD (hylo): +26.5 bps (z=-1.4) EXP richer
  ⚪ ONyc (onre): +774.4 bps (z=+1.4)

⏰ Expiring Within 30 Days
   22d  hylo · hyUSD · 29Mar26 [RTX]

ℹ️ 26 markets (14 EXP, 12 RTX) · Total TVL $332.1M
```

## Setup

### 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the bot token

### 2. Get your chat ID

1. Send any message to your new bot
2. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Find `"chat":{"id": XXXXXXX}` in the response

### 3. Configure and run

```bash
git clone https://github.com/<your-username>/PT_monitor.git
cd PT_monitor

cp .env.example .env
# Edit .env with your bot token and chat ID

pip install -r requirements.txt
python pt_monitor.py
```

## Project Structure

```
pt_monitor.py          Main bot — scraper + Telegram commands + alerts + daily report
ratex_scraper.py       Rate-X market data via RPC API
spread_signal.py       Cross-venue PT spread signal engine
analyze/scripts/       Historical analysis & data export tools
data/                  Snapshots, alerts CSV, daily JSON dumps
tasks/                 Task plans and lessons learned
```

## Analysis Tools

Export 30-day hourly PT/YT history for cross-venue comparison:

```bash
python analyze/scripts/comprehensive_market_report.py
```

Output: `analyze/output/comprehensive_YYYYMMDD_HHMMSS/` with per-market charts, spread analysis, and opportunity ranking.

## Supported Protocols

Markets are automatically grouped by protocol. Current mappings include:

| Protocol | Assets |
|----------|--------|
| Hylo | xSOL, hyloSOL, hyloSOL+, hyUSD, sHYUSD |
| Fragmetric | fragSOL, fragBTC, fragJTO |
| OnRe | ONyc |
| Jupiter | JLP, jlUSDC |
| Ethena | USDe, sUSDe |
| Unitas | sUSDu |
| Huma | PST |
| Perena | USD* |
| Kyros | kySOL, kyJTO |
| Kamino | kUSDC |
| Adrastea | lrtsSOL, adraSOL |
| Renzo | ezSOL |
| Yala | YU |
| And more... | |

## Tech Stack

- **Python 3.10+**
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) — async Telegram bot framework
- [requests](https://docs.python-requests.org/) — HTTP client for scraping
- [python-dotenv](https://github.com/theskumar/python-dotenv) — environment variable loading

## License

MIT
