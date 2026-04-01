from __future__ import annotations

import re
import time
from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .db import Database
from .paper import unrealized_pnl
from .polymarket import PolymarketClient
from .services import WalletSyncService
from .types import PaperPosition, Subscription, short_wallet


WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _fmt_decimal(value: Decimal, places: int = 4) -> str:
    quant = Decimal("1") if places == 0 else Decimal("1") / (Decimal("10") ** places)
    normalized = value.quantize(quant)
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


class TelegramUI:
    def __init__(self, db: Database, api: PolymarketClient, sync_service: WalletSyncService) -> None:
        self._db = db
        self._api = api
        self._sync_service = sync_service
        self._last_portfolio_refresh: dict[int, int] = {}

    def register_handlers(self, application: Application) -> None:
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("wallets", self.wallets_command))
        application.add_handler(CommandHandler("portfolio", self.portfolio_command))
        application.add_handler(CallbackQueryHandler(self.on_callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.effective_chat is None or update.message is None:
            return
        await self._db.upsert_user(update.effective_user.id, update.effective_chat.id)
        await update.message.reply_text(
            text=(
                "Polymarket follower bot is ready.\n"
                "Use buttons to add proxy wallets, configure filters, and enable paper copy mode."
            ),
            reply_markup=self._main_menu_markup(),
        )

    async def wallets_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.effective_chat is None or update.message is None:
            return
        await self._db.upsert_user(update.effective_user.id, update.effective_chat.id)
        text, markup = await self._build_wallets_view(update.effective_user.id)
        await update.message.reply_text(text=text, reply_markup=markup)

    async def portfolio_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.effective_chat is None or update.message is None:
            return
        await self._db.upsert_user(update.effective_user.id, update.effective_chat.id)
        text, markup = await self._build_portfolio_view(update.effective_user.id)
        await update.message.reply_text(text=text, reply_markup=markup)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.callback_query is None or update.effective_user is None or update.effective_chat is None:
            return

        query = update.callback_query
        await query.answer()
        await self._db.upsert_user(update.effective_user.id, update.effective_chat.id)

        data = query.data or ""
        parts = data.split(":")
        action = parts[0] if parts else ""

        if data == "menu:main":
            await self._safe_edit(query, "Main menu", self._main_menu_markup())
            return

        if data == "menu:wallets":
            text, markup = await self._build_wallets_view(update.effective_user.id)
            await self._safe_edit(query, text, markup)
            return

        if data == "menu:portfolio":
            text, markup = await self._build_portfolio_view(update.effective_user.id)
            await self._safe_edit(query, text, markup)
            return

        if data == "wallet:add":
            context.user_data["pending"] = {"action": "add_wallet"}
            if query.message:
                await query.message.reply_text("Send proxy wallet address (0x...) to follow.")
            return

        if action == "wallet" and len(parts) >= 3:
            sub_id = self._parse_int(parts[2])
            if sub_id is None:
                return

            if parts[1] == "view":
                await self._show_wallet_detail(query, update.effective_user.id, sub_id)
                return
            if parts[1] == "remove":
                await self._db.remove_subscription(sub_id, update.effective_user.id)
                await self._sync_service.request_sync()
                text, markup = await self._build_wallets_view(update.effective_user.id)
                await self._safe_edit(query, text, markup)
                return
            if parts[1] == "toggle_enabled":
                sub = await self._db.get_subscription(sub_id, update.effective_user.id)
                if sub:
                    await self._db.set_enabled(sub_id, update.effective_user.id, not sub.enabled)
                    await self._sync_service.request_sync()
                    await self._show_wallet_detail(query, update.effective_user.id, sub_id)
                return
            if parts[1] == "toggle_alerts":
                sub = await self._db.get_subscription(sub_id, update.effective_user.id)
                if sub:
                    await self._db.set_alerts_enabled(sub_id, update.effective_user.id, not sub.alerts_enabled)
                    await self._sync_service.request_sync()
                    await self._show_wallet_detail(query, update.effective_user.id, sub_id)
                return
            if parts[1] == "toggle_paper":
                sub = await self._db.get_subscription(sub_id, update.effective_user.id)
                if sub:
                    await self._db.set_paper_enabled(sub_id, update.effective_user.id, not sub.paper_enabled)
                    await self._sync_service.request_sync()
                    await self._show_wallet_detail(query, update.effective_user.id, sub_id)
                return

        if action == "filter" and len(parts) >= 3:
            sub_id = self._parse_int(parts[2])
            if sub_id is None:
                return

            filter_action = parts[1]
            if filter_action == "menu":
                await self._show_filter_detail(query, update.effective_user.id, sub_id)
                return
            if filter_action == "side":
                await self._db.cycle_side_filter(sub_id, update.effective_user.id)
                await self._show_filter_detail(query, update.effective_user.id, sub_id)
                return
            if filter_action == "outcome":
                await self._db.cycle_outcome_filter(sub_id, update.effective_user.id)
                await self._show_filter_detail(query, update.effective_user.id, sub_id)
                return
            if filter_action == "setmin":
                context.user_data["pending"] = {
                    "action": "set_price",
                    "which": "min",
                    "subscription_id": sub_id,
                }
                if query.message:
                    await query.message.reply_text("Send min price in cents (0-100).")
                return
            if filter_action == "setmax":
                context.user_data["pending"] = {
                    "action": "set_price",
                    "which": "max",
                    "subscription_id": sub_id,
                }
                if query.message:
                    await query.message.reply_text("Send max price in cents (0-100).")
                return
            if filter_action == "clear":
                await self._db.clear_price_range(sub_id, update.effective_user.id)
                await self._show_filter_detail(query, update.effective_user.id, sub_id)
                return

        if action == "portfolio" and len(parts) >= 2:
            portfolio_action = parts[1]
            if portfolio_action == "main":
                text, markup = await self._build_portfolio_view(update.effective_user.id)
                await self._safe_edit(query, text, markup)
                return
            if portfolio_action == "refresh":
                updated = await self._refresh_user_midpoints(update.effective_user.id)
                self._last_portfolio_refresh[update.effective_user.id] = int(time.time())
                text, markup = await self._build_portfolio_view(update.effective_user.id)
                if updated == 0:
                    text += "\n\nNo open paper positions to refresh."
                await self._safe_edit(query, text, markup)
                return
            if portfolio_action == "positions":
                text, markup = await self._build_positions_view(update.effective_user.id)
                await self._safe_edit(query, text, markup)
                return

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.effective_chat is None or update.message is None:
            return

        await self._db.upsert_user(update.effective_user.id, update.effective_chat.id)
        pending = context.user_data.get("pending")
        if not pending:
            return

        action = pending.get("action")
        text = update.message.text.strip()

        if action == "add_wallet":
            if not WALLET_RE.match(text):
                await update.message.reply_text("Invalid wallet format. Send a valid 0x... address.")
                return

            await self._db.add_subscription(update.effective_user.id, text)
            context.user_data.pop("pending", None)
            await self._sync_service.request_sync()

            wallets_text, wallets_markup = await self._build_wallets_view(update.effective_user.id)
            await update.message.reply_text("Wallet added.")
            await update.message.reply_text(wallets_text, reply_markup=wallets_markup)
            return

        if action == "set_price":
            sub_id = self._parse_int(str(pending.get("subscription_id")))
            which = str(pending.get("which") or "")
            if sub_id is None or which not in {"min", "max"}:
                context.user_data.pop("pending", None)
                await update.message.reply_text("Price update cancelled.")
                return

            try:
                value = int(text)
            except ValueError:
                await update.message.reply_text("Send an integer between 0 and 100.")
                return

            if value < 0 or value > 100:
                await update.message.reply_text("Price must be between 0 and 100 cents.")
                return

            sub = await self._db.get_subscription(sub_id, update.effective_user.id)
            if sub is None:
                context.user_data.pop("pending", None)
                await update.message.reply_text("Subscription not found.")
                return

            if which == "min" and sub.max_price_cents is not None and value > sub.max_price_cents:
                await update.message.reply_text("Min price cannot be higher than max price.")
                return
            if which == "max" and sub.min_price_cents is not None and value < sub.min_price_cents:
                await update.message.reply_text("Max price cannot be lower than min price.")
                return

            if which == "min":
                await self._db.set_min_price_cents(sub_id, update.effective_user.id, value)
            else:
                await self._db.set_max_price_cents(sub_id, update.effective_user.id, value)

            context.user_data.pop("pending", None)
            await update.message.reply_text(f"{which.upper()} price set to {value}c.")

            refreshed_sub = await self._db.get_subscription(sub_id, update.effective_user.id)
            if refreshed_sub is None:
                return
            text_out, markup = self._build_filter_view(refreshed_sub)
            await update.message.reply_text(text_out, reply_markup=markup)

    async def _build_wallets_view(self, telegram_user_id: int) -> tuple[str, InlineKeyboardMarkup]:
        subscriptions = await self._db.list_user_subscriptions(telegram_user_id)

        if not subscriptions:
            text = "No followed wallets yet. Tap Add Wallet to start."
        else:
            lines = ["Followed wallets:"]
            for sub in subscriptions:
                label = sub.alias or short_wallet(sub.wallet_address)
                modes = []
                if sub.alerts_enabled:
                    modes.append("alerts")
                if sub.paper_enabled:
                    modes.append("paper")
                mode_text = "+".join(modes) if modes else "off"
                lines.append(
                    f"- {label} | {'on' if sub.enabled else 'off'} | {mode_text} | "
                    f"{sub.side_filter.value}/{sub.outcome_filter.value}"
                )
            text = "\n".join(lines)

        keyboard = []
        for sub in subscriptions[:20]:
            label = sub.alias or short_wallet(sub.wallet_address)
            keyboard.append([InlineKeyboardButton(label, callback_data=f"wallet:view:{sub.id}")])

        keyboard.append([InlineKeyboardButton("+ Add Wallet", callback_data="wallet:add")])
        keyboard.append([InlineKeyboardButton("Back", callback_data="menu:main")])
        return text, InlineKeyboardMarkup(keyboard)

    async def _show_wallet_detail(self, query, telegram_user_id: int, sub_id: int) -> None:
        sub = await self._db.get_subscription(sub_id, telegram_user_id)
        if sub is None:
            await self._safe_edit(query, "Wallet not found.", self._main_menu_markup())
            return

        label = sub.alias or short_wallet(sub.wallet_address)
        text = "\n".join(
            [
                f"Wallet: {label}",
                f"Address: {sub.wallet_address}",
                f"Enabled: {'ON' if sub.enabled else 'OFF'}",
                f"Alerts: {'ON' if sub.alerts_enabled else 'OFF'}",
                f"Paper copy: {'ON' if sub.paper_enabled else 'OFF'}",
                f"Filters: side={sub.side_filter.value}, outcome={sub.outcome_filter.value}, "
                f"price={self._price_range_text(sub)}",
            ]
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"Enabled: {'ON' if sub.enabled else 'OFF'}",
                        callback_data=f"wallet:toggle_enabled:{sub.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"Alerts: {'ON' if sub.alerts_enabled else 'OFF'}",
                        callback_data=f"wallet:toggle_alerts:{sub.id}",
                    ),
                    InlineKeyboardButton(
                        f"Paper: {'ON' if sub.paper_enabled else 'OFF'}",
                        callback_data=f"wallet:toggle_paper:{sub.id}",
                    ),
                ],
                [InlineKeyboardButton("Edit Filters", callback_data=f"filter:menu:{sub.id}")],
                [InlineKeyboardButton("Remove Wallet", callback_data=f"wallet:remove:{sub.id}")],
                [InlineKeyboardButton("Back", callback_data="menu:wallets")],
            ]
        )
        await self._safe_edit(query, text, keyboard)

    async def _show_filter_detail(self, query, telegram_user_id: int, sub_id: int) -> None:
        sub = await self._db.get_subscription(sub_id, telegram_user_id)
        if sub is None:
            await self._safe_edit(query, "Wallet not found.", self._main_menu_markup())
            return
        text, markup = self._build_filter_view(sub)
        await self._safe_edit(query, text, markup)

    def _build_filter_view(self, sub: Subscription) -> tuple[str, InlineKeyboardMarkup]:
        text = "\n".join(
            [
                f"Filters for {sub.alias or short_wallet(sub.wallet_address)}",
                f"- Side: {sub.side_filter.value}",
                f"- Outcome: {sub.outcome_filter.value}",
                f"- Price range: {self._price_range_text(sub)}",
            ]
        )

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(f"Side: {sub.side_filter.value}", callback_data=f"filter:side:{sub.id}")],
                [
                    InlineKeyboardButton(
                        f"Outcome: {sub.outcome_filter.value}",
                        callback_data=f"filter:outcome:{sub.id}",
                    )
                ],
                [
                    InlineKeyboardButton("Set Min", callback_data=f"filter:setmin:{sub.id}"),
                    InlineKeyboardButton("Set Max", callback_data=f"filter:setmax:{sub.id}"),
                ],
                [InlineKeyboardButton("Clear Price Range", callback_data=f"filter:clear:{sub.id}")],
                [InlineKeyboardButton("Back", callback_data=f"wallet:view:{sub.id}")],
            ]
        )
        return text, keyboard

    async def _build_portfolio_view(self, telegram_user_id: int) -> tuple[str, InlineKeyboardMarkup]:
        summary = await self._db.get_user_portfolio_summary(telegram_user_id)
        refreshed_at = self._last_portfolio_refresh.get(telegram_user_id)
        refresh_text = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(refreshed_at)) if refreshed_at else "never"

        text = "\n".join(
            [
                "Paper portfolio",
                f"- Realized PnL: {_fmt_decimal(summary.realized, 4)}",
                f"- Unrealized PnL: {_fmt_decimal(summary.unrealized, 4)}",
                f"- Total PnL: {_fmt_decimal(summary.total, 4)}",
                f"- Open positions: {summary.open_positions}",
                f"- Last refresh: {refresh_text} UTC",
            ]
        )

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Refresh PnL", callback_data="portfolio:refresh")],
                [InlineKeyboardButton("Open Positions", callback_data="portfolio:positions")],
                [InlineKeyboardButton("Back", callback_data="menu:main")],
            ]
        )
        return text, keyboard

    async def _build_positions_view(self, telegram_user_id: int) -> tuple[str, InlineKeyboardMarkup]:
        positions = await self._db.get_user_paper_positions(telegram_user_id, only_open=True)
        if not positions:
            text = "No open paper positions."
        else:
            lines = ["Open paper positions:"]
            for pos in positions[:30]:
                lines.append(self._position_line(pos))
            if len(positions) > 30:
                lines.append(f"... and {len(positions) - 30} more")
            text = "\n".join(lines)

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Refresh PnL", callback_data="portfolio:refresh")],
                [InlineKeyboardButton("Back", callback_data="portfolio:main")],
            ]
        )
        return text, keyboard

    async def _refresh_user_midpoints(self, telegram_user_id: int) -> int:
        positions = await self._db.get_user_paper_positions(telegram_user_id, only_open=True)
        assets = sorted({pos.asset for pos in positions})
        if not assets:
            return 0

        marks = await self._api.get_midpoints(assets)
        await self._db.update_marks_for_user(telegram_user_id, marks)
        return len(marks)

    @staticmethod
    def _main_menu_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Wallets", callback_data="menu:wallets")],
                [InlineKeyboardButton("Paper Portfolio", callback_data="menu:portfolio")],
            ]
        )

    @staticmethod
    def _price_range_text(sub: Subscription) -> str:
        left = f"{sub.min_price_cents}c" if sub.min_price_cents is not None else "any"
        right = f"{sub.max_price_cents}c" if sub.max_price_cents is not None else "any"
        return f"{left}..{right}"

    @staticmethod
    def _position_line(pos: PaperPosition) -> str:
        mark = pos.last_mark_price
        upnl_text = "n/a"
        mark_text = "n/a"
        if mark is not None:
            upnl = unrealized_pnl(pos.qty, pos.avg_price, mark)
            upnl_text = _fmt_decimal(upnl, 4)
            mark_text = _fmt_decimal(mark, 4)
        wallet_label = pos.alias or short_wallet(pos.wallet_address)
        return (
            f"- {wallet_label} | {pos.outcome or '-'} | qty={_fmt_decimal(pos.qty, 4)} "
            f"avg={_fmt_decimal(pos.avg_price, 4)} mark={mark_text} uPnL={upnl_text}"
        )

    @staticmethod
    def _parse_int(value: str) -> int | None:
        try:
            return int(value)
        except ValueError:
            return None

    @staticmethod
    async def _safe_edit(query, text: str, markup: InlineKeyboardMarkup) -> None:
        try:
            await query.edit_message_text(text=text, reply_markup=markup)
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                return
            raise
