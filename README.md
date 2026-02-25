# ExponentFi Yield Monitor

Telegram bot that tracks all [Exponent Finance](https://www.exponent.finance) yield trading markets in real-time — PT/YT prices, implied APY, pool liquidity, and more.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)

## Features

- **All markets dashboard** — compact overview of every active market, grouped by platform
- **Single market detail** — deep view with PT/YT prices, implied APY, underlying yield, and pool stats
- **Always-on alerts** — automatic notifications for:
  - New market listings
  - PT price or implied APY moves >8%
  - LP pool fill crossing 80%
- **Custom alerts** — set your own percentage thresholds, optionally filtered by market
- **Periodic updates** — subscribe to receive the dashboard every 10 minutes

## How It Works

Exponent Finance has no public API. The bot scrapes the dehydrated React Server Component state embedded in the HTML — a single page load returns data for **all markets**. Prices, APYs, and liquidity figures are extracted via regex-based parsing and cross-referenced against the on-page token map for accurate USD conversion.

## Commands

| Command | Description |
|---------|-------------|
| `/markets` | Show all active markets dashboard |
| `/market <filter>` | Detailed view of a specific market (e.g. `/market bulk`) |
| `/setalert <pct> [market]` | Alert on % change (e.g. `/setalert 5 bulk`) |
| `/alerts` | List your active alerts |
| `/deletealert <id>` | Remove an alert |
| `/subscribe` | Start receiving periodic dashboards |
| `/unsubscribe` | Stop periodic dashboards |

## Dashboard Preview

```
📊 ExponentFi Yield Monitor
   16 active markets

━━━ BULK ━━━
▸ bulk · BulkSOL · 20Jun26
   PT 0.9658 ($78.65) · 11.2% apy
   YT 0.0342 ($2.78) · 5.9% yield
   Pool ▓░░░░░░░ 22% — $548K/$2.5M

━━━ HYLO ━━━
▸ hylo · hyloSOL · 14Apr26
   PT 0.9898 ($80.61) · 7.8% apy
   YT 0.0102 ($0.83) · 6.5% yield
   Pool ▓▓▓▓░░░░ 55% — $1.4M/$2.5M
...
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

# Set up environment
cp .env.example .env
# Edit .env with your bot token and chat ID

# Install dependencies
pip install -r requirements.txt

# Run
python pt_monitor.py
```

## Tech Stack

- **Python 3.10+**
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) — async Telegram bot framework
- [requests](https://docs.python-requests.org/) — HTTP client for scraping
- [python-dotenv](https://github.com/theskumar/python-dotenv) — environment variable loading

## License

MIT