from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request

from backend.app.core.config import Settings


class GlobalMarketIntel:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.status = "disabled" if not settings.global_market_enabled else "warming"
        self.error = ""
        self.updated_at = 0
        self.data = {
            "btcUsd": 0,
            "ethUsd": 0,
            "btcChange24h": 0,
            "ethChange24h": 0,
            "btcMarketCap": 0,
            "ethMarketCap": 0,
            "source": "CoinGecko",
        }
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.settings.global_market_enabled or self.task:
            return
        self.task = asyncio.create_task(self.loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            self.task = None

    async def loop(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.settings.global_market_interval_ms / 1000)

    async def refresh(self) -> None:
        try:
            payload = await asyncio.to_thread(self.fetch_coingecko)
            bitcoin = payload.get("bitcoin", {})
            ethereum = payload.get("ethereum", {})
            self.data = {
                "btcUsd": bitcoin.get("usd", 0) or 0,
                "ethUsd": ethereum.get("usd", 0) or 0,
                "btcChange24h": bitcoin.get("usd_24h_change", 0) or 0,
                "ethChange24h": ethereum.get("usd_24h_change", 0) or 0,
                "btcMarketCap": bitcoin.get("usd_market_cap", 0) or 0,
                "ethMarketCap": ethereum.get("usd_market_cap", 0) or 0,
                "source": "CoinGecko",
            }
            self.status = "online"
            self.error = ""
            self.updated_at = int(time.time() * 1000)
        except Exception as exc:  # pragma: no cover - external network
            self.status = "offline"
            self.error = str(exc)

    def fetch_coingecko(self) -> dict:
        url = (
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin,ethereum"
            "&vs_currencies=usd"
            "&include_market_cap=true"
            "&include_24hr_change=true"
            "&include_last_updated_at=true"
        )
        request = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "aurelion/2.1"})
        with urllib.request.urlopen(request, timeout=4) as response:
            return json.loads(response.read().decode("utf-8"))

    def snapshot(self) -> dict:
        return {
            "enabled": self.settings.global_market_enabled,
            "status": self.status,
            "error": self.error,
            "updatedAt": self.updated_at,
            **self.data,
        }
