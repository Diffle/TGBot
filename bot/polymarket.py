from __future__ import annotations

import asyncio
from decimal import Decimal
import logging
from typing import Any

import aiohttp

from .config import Config
from .types import TradeEvent, normalize_wallet


logger = logging.getLogger(__name__)


class PolymarketClient:
    def __init__(self, config: Config) -> None:
        self._data_api_base = config.data_api_base.rstrip("/")
        self._clob_base = config.clob_base.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=config.request_timeout_seconds)
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_user_trades(self, wallet_address: str, limit: int = 80) -> list[TradeEvent]:
        payload = await self._request_json(
            self._data_api_base,
            "/trades",
            params={"user": normalize_wallet(wallet_address), "limit": str(limit)},
        )
        return self._parse_trades(payload)

    async def get_market_trades(self, condition_id: str, limit: int = 50) -> list[TradeEvent]:
        payload = await self._request_json(
            self._data_api_base,
            "/trades",
            params={"market": condition_id, "limit": str(limit)},
        )
        return self._parse_trades(payload)

    async def get_midpoints(self, asset_ids: list[str]) -> dict[str, Decimal]:
        if not asset_ids:
            return {}

        body = [{"token_id": asset_id} for asset_id in asset_ids]
        payload = await self._request_json(
            self._clob_base,
            "/midpoints",
            method="POST",
            json_body=body,
        )

        result: dict[str, Decimal] = {}
        if isinstance(payload, dict):
            for asset_id, value in payload.items():
                try:
                    result[str(asset_id)] = Decimal(str(value))
                except Exception:
                    continue
        return result

    async def _request_json(
        self,
        base_url: str,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, str] | None = None,
        json_body: Any | None = None,
    ) -> Any:
        session = await self._get_session()
        url = f"{base_url}/{path.lstrip('/')}"

        for attempt in range(3):
            try:
                async with session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_body,
                    timeout=self._timeout,
                ) as response:
                    if response.status in {429, 500, 502, 503, 504} and attempt < 2:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    response.raise_for_status()
                    return await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt == 2:
                    raise
                logger.warning("request failed (%s), retrying", exc)
                await asyncio.sleep(0.5 * (attempt + 1))

        return []

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _parse_trades(self, payload: Any) -> list[TradeEvent]:
        if not isinstance(payload, list):
            return []

        trades: list[TradeEvent] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue

            try:
                proxy_wallet = normalize_wallet(str(raw.get("proxyWallet") or ""))
                side = str(raw.get("side") or "").upper()
                asset = str(raw.get("asset") or raw.get("assetId") or "")
                condition_id = str(raw.get("conditionId") or raw.get("market") or "")
                size = Decimal(str(raw.get("size") or "0"))
                price = Decimal(str(raw.get("price") or "0"))
                timestamp = int(raw.get("timestamp") or 0)
                transaction_hash = str(raw.get("transactionHash") or "")
                title = str(raw.get("title") or "")
                slug = str(raw.get("slug") or "")
                outcome = str(raw.get("outcome") or "")
            except Exception:
                continue

            if not proxy_wallet or not side or not asset or not condition_id:
                continue

            trades.append(
                TradeEvent(
                    proxy_wallet=proxy_wallet,
                    side=side,
                    asset=asset,
                    condition_id=condition_id,
                    size=size,
                    price=price,
                    timestamp=timestamp,
                    title=title,
                    slug=slug,
                    outcome=outcome,
                    transaction_hash=transaction_hash,
                )
            )

        return trades
