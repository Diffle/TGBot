from __future__ import annotations

from dataclasses import dataclass
import os


def _int_env(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    return max(parsed, minimum)


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: str
    data_api_base: str
    clob_base: str
    ws_url: str
    wallet_sync_seconds: int
    wallet_backfill_limit: int
    ws_market_lookup_limit: int
    max_ws_assets: int
    request_timeout_seconds: int

    @staticmethod
    def from_env() -> "Config":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if not bot_token:
            raise RuntimeError("BOT_TOKEN is required")

        return Config(
            bot_token=bot_token,
            db_path=os.getenv("DB_PATH", "bot.db"),
            data_api_base=os.getenv("DATA_API_BASE", "https://data-api.polymarket.com"),
            clob_base=os.getenv("CLOB_BASE", "https://clob.polymarket.com"),
            ws_url=os.getenv("WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
            wallet_sync_seconds=_int_env("WALLET_SYNC_SECONDS", 45, 10),
            wallet_backfill_limit=_int_env("WALLET_BACKFILL_LIMIT", 80, 10),
            ws_market_lookup_limit=_int_env("WS_MARKET_LOOKUP_LIMIT", 50, 10),
            max_ws_assets=_int_env("MAX_WS_ASSETS", 600, 20),
            request_timeout_seconds=_int_env("REQUEST_TIMEOUT_SECONDS", 15, 5),
        )
