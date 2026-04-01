from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import Application

from .config import Config
from .db import Database
from .polymarket import PolymarketClient
from .services import TradeProcessor, WalletSyncService, WebSocketTradeStreamer
from .telegram_ui import TelegramUI


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    config = Config.from_env()
    db = Database(config.db_path)
    api = PolymarketClient(config)
    processor = TradeProcessor(db)
    websocket_streamer = WebSocketTradeStreamer(config, api, processor)
    wallet_sync = WalletSyncService(config, db, api, processor, websocket_streamer)
    ui = TelegramUI(db, api, wallet_sync)

    async def post_init(application: Application) -> None:
        await db.init()
        processor.set_bot(application.bot)

        background_tasks = [
            application.create_task(wallet_sync.run(), name="wallet-sync"),
            application.create_task(websocket_streamer.run(), name="websocket-stream"),
        ]
        application.bot_data["background_tasks"] = background_tasks
        await wallet_sync.request_sync()

    async def post_shutdown(application: Application) -> None:
        await wallet_sync.stop()
        await websocket_streamer.stop()

        tasks = application.bot_data.get("background_tasks", [])
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        await api.close()

    application = (
        Application.builder()
        .token(config.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    ui.register_handlers(application)

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
