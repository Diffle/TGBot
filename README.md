# Polymarket Wallet Follower Bot

Telegram bot for Linux VPS that tracks Polymarket proxy wallets, sends filtered trade notifications, and supports a paper copy mode with midpoint-based PnL refresh.

## What it does

- Follow one or more Polymarket proxy wallets from Telegram buttons.
- Configure per-wallet filters from inline buttons:
  - side (`ANY`, `BUY`, `SELL`)
  - outcome (`ANY`, `YES`, `NO`)
  - min/max price in cents
- Receive notifications only for matching trades.
- Enable `paper copy` mode per wallet:
  - mirrors matching upcoming trades into a virtual portfolio
  - tracks realized/unrealized/total PnL
  - `Refresh PnL` button updates marks using Polymarket midpoint prices (`/midpoints`).

## Architecture

- Telegram interface: `python-telegram-bot` v22.5 (inline keyboard + callback handlers).
- Trade detection:
  - WebSocket stream (`ws-subscriptions-clob.polymarket.com`) for `last_trade_price` events.
  - Event resolution through Data API market trades (`data-api.polymarket.com/trades?market=...`).
  - Periodic backfill sync per followed wallet to catch misses and discover new assets.
- Storage: SQLite (`aiosqlite`) with idempotent seen-trade keys.

## Quick start

1. Create bot in BotFather and copy token.
2. Set up environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

3. Edit `.env` and set `BOT_TOKEN`.
4. Run:

```bash
python main.py
```

## systemd example

```ini
[Unit]
Description=Polymarket Follower Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/polymarket-bot
EnvironmentFile=/opt/polymarket-bot/.env
ExecStart=/opt/polymarket-bot/.venv/bin/python /opt/polymarket-bot/main.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

## Notes

- Outcome filter `YES/NO` is strict string matching against incoming trade outcome labels.
- Paper PnL is midpoint-marked; if no midpoint is available for an asset, unrealized PnL for that position stays unchanged until next refresh.
