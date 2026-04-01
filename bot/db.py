from __future__ import annotations

import time
from decimal import Decimal

import aiosqlite

from .paper import apply_fill, unrealized_pnl
from .types import OutcomeFilter, PaperPosition, PortfolioSummary, SideFilter, Subscription, TradeEvent, normalize_wallet


def _now_ts() -> int:
    return int(time.time())


def _to_bool(value: int) -> bool:
    return bool(int(value))


class Database:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    wallet_address TEXT NOT NULL,
                    alias TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    alerts_enabled INTEGER NOT NULL DEFAULT 1,
                    paper_enabled INTEGER NOT NULL DEFAULT 0,
                    side_filter TEXT NOT NULL DEFAULT 'ANY',
                    outcome_filter TEXT NOT NULL DEFAULT 'ANY',
                    min_price_cents INTEGER,
                    max_price_cents INTEGER,
                    start_timestamp INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(telegram_user_id, wallet_address),
                    FOREIGN KEY(telegram_user_id) REFERENCES users(telegram_user_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_subscriptions_wallet
                ON subscriptions(wallet_address);

                CREATE INDEX IF NOT EXISTS idx_subscriptions_active
                ON subscriptions(enabled, alerts_enabled, paper_enabled);

                CREATE TABLE IF NOT EXISTS seen_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    trade_key TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(subscription_id, trade_key),
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    trade_key TEXT NOT NULL,
                    wallet_address TEXT NOT NULL,
                    side TEXT NOT NULL,
                    outcome TEXT,
                    asset TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    size TEXT NOT NULL,
                    price TEXT NOT NULL,
                    title TEXT,
                    timestamp INTEGER NOT NULL,
                    transaction_hash TEXT,
                    created_at INTEGER NOT NULL,
                    UNIQUE(subscription_id, trade_key),
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS paper_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    asset TEXT NOT NULL,
                    outcome TEXT,
                    qty TEXT NOT NULL,
                    avg_price TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL,
                    last_mark_price TEXT,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(subscription_id, asset),
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
                );
                """
            )
            await db.commit()

    async def upsert_user(self, telegram_user_id: int, chat_id: int) -> None:
        now = _now_ts()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO users (telegram_user_id, chat_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id)
                DO UPDATE SET chat_id = excluded.chat_id, updated_at = excluded.updated_at
                """,
                (telegram_user_id, chat_id, now, now),
            )
            await db.commit()

    async def add_subscription(self, telegram_user_id: int, wallet_address: str) -> Subscription:
        wallet = normalize_wallet(wallet_address)
        now = _now_ts()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                """
                INSERT OR IGNORE INTO subscriptions (
                    telegram_user_id,
                    wallet_address,
                    alias,
                    enabled,
                    alerts_enabled,
                    paper_enabled,
                    side_filter,
                    outcome_filter,
                    min_price_cents,
                    max_price_cents,
                    start_timestamp,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, NULL, 1, 1, 0, 'ANY', 'ANY', NULL, NULL, ?, ?, ?)
                """,
                (telegram_user_id, wallet, now, now, now),
            )
            await db.commit()

        sub = await self.get_subscription_by_wallet(telegram_user_id, wallet)
        if sub is None:
            raise RuntimeError("failed to create subscription")
        return sub

    async def list_user_subscriptions(self, telegram_user_id: int) -> list[Subscription]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT s.*, u.chat_id
                FROM subscriptions s
                JOIN users u ON u.telegram_user_id = s.telegram_user_id
                WHERE s.telegram_user_id = ?
                ORDER BY s.created_at DESC
                """,
                (telegram_user_id,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_subscription(row) for row in rows]

    async def get_subscription(self, subscription_id: int, telegram_user_id: int) -> Subscription | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT s.*, u.chat_id
                FROM subscriptions s
                JOIN users u ON u.telegram_user_id = s.telegram_user_id
                WHERE s.id = ? AND s.telegram_user_id = ?
                """,
                (subscription_id, telegram_user_id),
            )
            row = await cursor.fetchone()
        return self._row_to_subscription(row) if row else None

    async def get_subscription_by_wallet(self, telegram_user_id: int, wallet_address: str) -> Subscription | None:
        wallet = normalize_wallet(wallet_address)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT s.*, u.chat_id
                FROM subscriptions s
                JOIN users u ON u.telegram_user_id = s.telegram_user_id
                WHERE s.telegram_user_id = ? AND s.wallet_address = ?
                """,
                (telegram_user_id, wallet),
            )
            row = await cursor.fetchone()
        return self._row_to_subscription(row) if row else None

    async def remove_subscription(self, subscription_id: int, telegram_user_id: int) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "DELETE FROM subscriptions WHERE id = ? AND telegram_user_id = ?",
                (subscription_id, telegram_user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def set_enabled(self, subscription_id: int, telegram_user_id: int, enabled: bool) -> None:
        await self._update_subscription_fields(
            subscription_id,
            telegram_user_id,
            {"enabled": 1 if enabled else 0},
        )

    async def set_alerts_enabled(self, subscription_id: int, telegram_user_id: int, enabled: bool) -> None:
        await self._update_subscription_fields(
            subscription_id,
            telegram_user_id,
            {"alerts_enabled": 1 if enabled else 0},
        )

    async def set_paper_enabled(self, subscription_id: int, telegram_user_id: int, enabled: bool) -> None:
        await self._update_subscription_fields(
            subscription_id,
            telegram_user_id,
            {"paper_enabled": 1 if enabled else 0},
        )

    async def cycle_side_filter(self, subscription_id: int, telegram_user_id: int) -> SideFilter:
        sub = await self.get_subscription(subscription_id, telegram_user_id)
        if sub is None:
            return SideFilter.ANY
        next_value = sub.side_filter.next_value()
        await self._update_subscription_fields(
            subscription_id,
            telegram_user_id,
            {"side_filter": next_value.value},
        )
        return next_value

    async def cycle_outcome_filter(self, subscription_id: int, telegram_user_id: int) -> OutcomeFilter:
        sub = await self.get_subscription(subscription_id, telegram_user_id)
        if sub is None:
            return OutcomeFilter.ANY
        next_value = sub.outcome_filter.next_value()
        await self._update_subscription_fields(
            subscription_id,
            telegram_user_id,
            {"outcome_filter": next_value.value},
        )
        return next_value

    async def set_min_price_cents(self, subscription_id: int, telegram_user_id: int, value: int | None) -> None:
        await self._update_subscription_fields(
            subscription_id,
            telegram_user_id,
            {"min_price_cents": value},
        )

    async def set_max_price_cents(self, subscription_id: int, telegram_user_id: int, value: int | None) -> None:
        await self._update_subscription_fields(
            subscription_id,
            telegram_user_id,
            {"max_price_cents": value},
        )

    async def clear_price_range(self, subscription_id: int, telegram_user_id: int) -> None:
        await self._update_subscription_fields(
            subscription_id,
            telegram_user_id,
            {"min_price_cents": None, "max_price_cents": None},
        )

    async def list_active_wallets(self) -> list[str]:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                SELECT DISTINCT wallet_address
                FROM subscriptions
                WHERE enabled = 1
                  AND (alerts_enabled = 1 OR paper_enabled = 1)
                """
            )
            rows = await cursor.fetchall()
        return [normalize_wallet(row[0]) for row in rows]

    async def list_subscriptions_for_wallet(self, wallet_address: str) -> list[Subscription]:
        wallet = normalize_wallet(wallet_address)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT s.*, u.chat_id
                FROM subscriptions s
                JOIN users u ON u.telegram_user_id = s.telegram_user_id
                WHERE s.wallet_address = ?
                  AND s.enabled = 1
                  AND (s.alerts_enabled = 1 OR s.paper_enabled = 1)
                """,
                (wallet,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_subscription(row) for row in rows]

    async def mark_trade_seen(self, subscription_id: int, trade_key: str) -> bool:
        now = _now_ts()
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO seen_trades (subscription_id, trade_key, created_at)
                VALUES (?, ?, ?)
                """,
                (subscription_id, trade_key, now),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def record_paper_trade(self, subscription_id: int, trade_key: str, trade: TradeEvent) -> bool:
        now = _now_ts()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN")

            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO paper_trades (
                    subscription_id,
                    trade_key,
                    wallet_address,
                    side,
                    outcome,
                    asset,
                    condition_id,
                    size,
                    price,
                    title,
                    timestamp,
                    transaction_hash,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subscription_id,
                    trade_key,
                    trade.proxy_wallet,
                    trade.side,
                    trade.outcome,
                    trade.asset,
                    trade.condition_id,
                    str(trade.size),
                    str(trade.price),
                    trade.title,
                    trade.timestamp,
                    trade.transaction_hash,
                    now,
                ),
            )
            if cursor.rowcount == 0:
                await db.rollback()
                return False

            position_cursor = await db.execute(
                """
                SELECT qty, avg_price, realized_pnl
                FROM paper_positions
                WHERE subscription_id = ? AND asset = ?
                """,
                (subscription_id, trade.asset),
            )
            row = await position_cursor.fetchone()

            qty = Decimal(row["qty"]) if row else Decimal("0")
            avg_price = Decimal(row["avg_price"]) if row else Decimal("0")
            realized_pnl = Decimal(row["realized_pnl"]) if row else Decimal("0")

            new_qty, new_avg, new_realized = apply_fill(
                qty,
                avg_price,
                realized_pnl,
                side=trade.side,
                size=trade.size,
                price=trade.price,
            )

            await db.execute(
                """
                INSERT INTO paper_positions (
                    subscription_id,
                    asset,
                    outcome,
                    qty,
                    avg_price,
                    realized_pnl,
                    last_mark_price,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(subscription_id, asset)
                DO UPDATE SET
                    outcome = excluded.outcome,
                    qty = excluded.qty,
                    avg_price = excluded.avg_price,
                    realized_pnl = excluded.realized_pnl,
                    updated_at = excluded.updated_at
                """,
                (
                    subscription_id,
                    trade.asset,
                    trade.outcome,
                    str(new_qty),
                    str(new_avg),
                    str(new_realized),
                    now,
                ),
            )

            await db.commit()
            return True

    async def get_user_paper_positions(
        self,
        telegram_user_id: int,
        *,
        only_open: bool,
    ) -> list[PaperPosition]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                    p.subscription_id,
                    p.asset,
                    p.outcome,
                    p.qty,
                    p.avg_price,
                    p.realized_pnl,
                    p.last_mark_price,
                    s.wallet_address,
                    s.alias
                FROM paper_positions p
                JOIN subscriptions s ON s.id = p.subscription_id
                WHERE s.telegram_user_id = ?
                ORDER BY p.updated_at DESC
                """,
                (telegram_user_id,),
            )
            rows = await cursor.fetchall()

        result: list[PaperPosition] = []
        for row in rows:
            qty = Decimal(row["qty"])
            if only_open and qty == 0:
                continue
            mark = Decimal(row["last_mark_price"]) if row["last_mark_price"] else None
            result.append(
                PaperPosition(
                    subscription_id=int(row["subscription_id"]),
                    wallet_address=str(row["wallet_address"]),
                    alias=str(row["alias"]) if row["alias"] else None,
                    asset=str(row["asset"]),
                    outcome=str(row["outcome"] or ""),
                    qty=qty,
                    avg_price=Decimal(row["avg_price"]),
                    realized_pnl=Decimal(row["realized_pnl"]),
                    last_mark_price=mark,
                )
            )
        return result

    async def update_marks_for_user(self, telegram_user_id: int, marks: dict[str, Decimal]) -> None:
        if not marks:
            return
        now = _now_ts()
        async with aiosqlite.connect(self._db_path) as db:
            for asset, mark in marks.items():
                await db.execute(
                    """
                    UPDATE paper_positions
                    SET last_mark_price = ?, updated_at = ?
                    WHERE asset = ?
                      AND subscription_id IN (
                        SELECT id FROM subscriptions WHERE telegram_user_id = ?
                      )
                    """,
                    (str(mark), now, asset, telegram_user_id),
                )
            await db.commit()

    async def get_user_portfolio_summary(self, telegram_user_id: int) -> PortfolioSummary:
        positions = await self.get_user_paper_positions(telegram_user_id, only_open=False)
        realized = sum((pos.realized_pnl for pos in positions), Decimal("0"))

        unrealized = Decimal("0")
        open_positions = 0
        for pos in positions:
            if pos.qty == 0:
                continue
            open_positions += 1
            if pos.last_mark_price is None:
                continue
            unrealized += unrealized_pnl(pos.qty, pos.avg_price, pos.last_mark_price)

        total = realized + unrealized
        return PortfolioSummary(
            realized=realized,
            unrealized=unrealized,
            total=total,
            open_positions=open_positions,
        )

    async def _update_subscription_fields(
        self,
        subscription_id: int,
        telegram_user_id: int,
        fields: dict[str, object],
    ) -> None:
        if not fields:
            return
        now = _now_ts()
        set_parts = [f"{name} = ?" for name in fields.keys()]
        values = list(fields.values())
        values.append(now)
        values.append(subscription_id)
        values.append(telegram_user_id)

        query = f"""
            UPDATE subscriptions
            SET {', '.join(set_parts)}, updated_at = ?
            WHERE id = ? AND telegram_user_id = ?
        """
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(query, tuple(values))
            await db.commit()

    def _row_to_subscription(self, row: aiosqlite.Row) -> Subscription:
        side_value = str(row["side_filter"] or SideFilter.ANY.value)
        outcome_value = str(row["outcome_filter"] or OutcomeFilter.ANY.value)

        side_filter = SideFilter(side_value) if side_value in SideFilter._value2member_map_ else SideFilter.ANY
        outcome_filter = (
            OutcomeFilter(outcome_value)
            if outcome_value in OutcomeFilter._value2member_map_
            else OutcomeFilter.ANY
        )

        return Subscription(
            id=int(row["id"]),
            telegram_user_id=int(row["telegram_user_id"]),
            chat_id=int(row["chat_id"]),
            wallet_address=str(row["wallet_address"]),
            alias=str(row["alias"]) if row["alias"] else None,
            enabled=_to_bool(row["enabled"]),
            alerts_enabled=_to_bool(row["alerts_enabled"]),
            paper_enabled=_to_bool(row["paper_enabled"]),
            side_filter=side_filter,
            outcome_filter=outcome_filter,
            min_price_cents=int(row["min_price_cents"]) if row["min_price_cents"] is not None else None,
            max_price_cents=int(row["max_price_cents"]) if row["max_price_cents"] is not None else None,
            start_timestamp=int(row["start_timestamp"]),
        )
