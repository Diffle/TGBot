from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets
from websockets.exceptions import ConnectionClosed

from telegram import Bot
from telegram.error import TelegramError

from .config import Config
from .db import Database
from .filters import trade_matches_subscription
from .polymarket import PolymarketClient
from .types import Subscription, TradeEvent, short_wallet


logger = logging.getLogger(__name__)


def _fmt_decimal(value) -> str:
    text = format(value.normalize(), "f") if hasattr(value, "normalize") else str(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


class TradeProcessor:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._bot: Bot | None = None

    def set_bot(self, bot: Bot) -> None:
        self._bot = bot

    async def process_trade(self, trade: TradeEvent, *, source: str) -> None:
        subscriptions = await self._db.list_subscriptions_for_wallet(trade.proxy_wallet)
        if not subscriptions:
            return

        for sub in subscriptions:
            if trade.timestamp < sub.start_timestamp:
                continue
            if not trade_matches_subscription(trade, sub):
                continue

            trade_key = self._build_trade_key(trade)
            is_new = await self._db.mark_trade_seen(sub.id, trade_key)
            if not is_new:
                continue

            paper_copied = False
            if sub.paper_enabled:
                paper_copied = await self._db.record_paper_trade(sub.id, trade_key, trade)

            if self._bot is None:
                continue

            if sub.alerts_enabled:
                await self._safe_send(sub.chat_id, self._format_alert_message(sub, trade, source, paper_copied))
            elif paper_copied:
                await self._safe_send(sub.chat_id, self._format_paper_message(sub, trade, source))

    async def _safe_send(self, chat_id: int, text: str) -> None:
        if self._bot is None:
            return
        try:
            await self._bot.send_message(chat_id=chat_id, text=text)
        except TelegramError:
            logger.exception("failed to send notification to chat %s", chat_id)

    @staticmethod
    def _build_trade_key(trade: TradeEvent) -> str:
        return ":".join(
            [
                trade.transaction_hash or "nohash",
                trade.asset,
                trade.side,
                str(trade.price),
                str(trade.size),
                str(trade.timestamp),
            ]
        )

    @staticmethod
    def _format_alert_message(sub: Subscription, trade: TradeEvent, source: str, paper_copied: bool) -> str:
        side_tag = "[BUY]" if trade.side == "BUY" else "[SELL]"
        wallet_label = sub.alias or short_wallet(sub.wallet_address)
        market_name = trade.title or trade.slug or trade.condition_id
        lines = [
            f"{side_tag} {trade.side} {trade.outcome or '-'} @ {trade.price_cents}c",
            f"Wallet: {wallet_label}",
            f"Market: {market_name}",
            f"Size: {_fmt_decimal(trade.size)}",
            f"Mode: {'alert+paper' if paper_copied else 'alert'}",
            f"Source: {source}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _format_paper_message(sub: Subscription, trade: TradeEvent, source: str) -> str:
        wallet_label = sub.alias or short_wallet(sub.wallet_address)
        market_name = trade.title or trade.slug or trade.condition_id
        lines = [
            f"[PAPER] copied: {trade.side} {trade.outcome or '-'} @ {trade.price_cents}c",
            f"Wallet: {wallet_label}",
            f"Market: {market_name}",
            f"Size: {_fmt_decimal(trade.size)}",
            f"Source: {source}",
        ]
        return "\n".join(lines)


class WebSocketTradeStreamer:
    def __init__(
        self,
        config: Config,
        api: PolymarketClient,
        processor: TradeProcessor,
    ) -> None:
        self._config = config
        self._api = api
        self._processor = processor
        self._assets: set[str] = set()
        self._asset_lock = asyncio.Lock()
        self._assets_changed = asyncio.Event()
        self._stop = asyncio.Event()
        self._recent_events: dict[str, float] = {}

    async def update_assets(self, assets: set[str]) -> None:
        filtered = {asset for asset in assets if asset}
        async with self._asset_lock:
            if filtered == self._assets:
                return
            self._assets = filtered
            self._assets_changed.set()

    async def stop(self) -> None:
        self._stop.set()
        self._assets_changed.set()

    async def run(self) -> None:
        backoff = 1

        while not self._stop.is_set():
            assets = await self._get_assets_snapshot()
            if not assets:
                await self._wait_for_assets()
                continue

            try:
                async with websockets.connect(
                    self._config.ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=2_000_000,
                ) as ws:
                    await ws.send(json.dumps({"assets_ids": assets, "type": "market"}))
                    logger.info("websocket subscribed: %s assets", len(assets))
                    backoff = 1
                    self._assets_changed.clear()

                    while not self._stop.is_set():
                        if self._assets_changed.is_set():
                            break

                        try:
                            payload = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        except ConnectionClosed:
                            break

                        await self._handle_payload(payload)
            except Exception:
                logger.exception("websocket connection failed")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _wait_for_assets(self) -> None:
        self._assets_changed.clear()
        try:
            await asyncio.wait_for(self._assets_changed.wait(), timeout=5)
        except asyncio.TimeoutError:
            return

    async def _get_assets_snapshot(self) -> list[str]:
        async with self._asset_lock:
            return sorted(self._assets)

    async def _handle_payload(self, payload: str) -> None:
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            return

        messages = decoded if isinstance(decoded, list) else [decoded]
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("event_type") != "last_trade_price":
                continue
            await self._resolve_trade_event(message)

    async def _resolve_trade_event(self, event: dict[str, object]) -> None:
        tx_hash = str(event.get("transaction_hash") or "").lower()
        market = str(event.get("market") or "")
        asset = str(event.get("asset_id") or "")
        if not tx_hash or not market:
            return

        event_key = f"{market}:{asset}:{tx_hash}"
        now = time.monotonic()
        previous = self._recent_events.get(event_key)
        if previous and (now - previous) < 120:
            return
        self._recent_events[event_key] = now
        if len(self._recent_events) > 5000:
            self._recent_events = {
                key: ts for key, ts in self._recent_events.items() if (now - ts) < 180
            }

        matching_trades: list[TradeEvent] = []
        for attempt in range(3):
            market_trades = await self._api.get_market_trades(
                market,
                limit=self._config.ws_market_lookup_limit,
            )
            matching_trades = [
                trade
                for trade in market_trades
                if trade.transaction_hash.lower() == tx_hash
                and (not asset or trade.asset == asset)
            ]
            if matching_trades:
                break
            await asyncio.sleep(0.35 * (attempt + 1))

        for trade in sorted(matching_trades, key=lambda item: item.timestamp):
            await self._processor.process_trade(trade, source="websocket")


class WalletSyncService:
    def __init__(
        self,
        config: Config,
        db: Database,
        api: PolymarketClient,
        processor: TradeProcessor,
        websocket_streamer: WebSocketTradeStreamer,
    ) -> None:
        self._config = config
        self._db = db
        self._api = api
        self._processor = processor
        self._websocket_streamer = websocket_streamer
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    async def request_sync(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._sync_once()
            except Exception:
                logger.exception("wallet sync failed")

            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._config.wallet_sync_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _sync_once(self) -> None:
        wallets = await self._db.list_active_wallets()
        if not wallets:
            await self._websocket_streamer.update_assets(set())
            return

        ordered_assets: list[str] = []
        seen_assets: set[str] = set()

        for wallet in wallets:
            trades = await self._api.get_user_trades(wallet, limit=self._config.wallet_backfill_limit)

            for trade in sorted(trades, key=lambda item: item.timestamp):
                await self._processor.process_trade(trade, source="backfill")

            for trade in trades:
                if trade.asset in seen_assets:
                    continue
                seen_assets.add(trade.asset)
                ordered_assets.append(trade.asset)

        limited_assets = set(ordered_assets[: self._config.max_ws_assets])
        await self._websocket_streamer.update_assets(limited_assets)
