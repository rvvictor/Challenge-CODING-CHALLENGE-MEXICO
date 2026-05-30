from __future__ import annotations

import json

from backend.app.core.config import Settings


class RedisBus:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = None
        self.enabled = bool(settings.redis_enabled and settings.redis_url)
        self.status = "disabled" if not self.enabled else "connecting"
        self.error = ""

    async def start(self) -> None:
        if not self.enabled:
            return
        try:
            import redis.asyncio as redis

            self.client = redis.from_url(self.settings.redis_url, decode_responses=True)
            await self.client.ping()
            self.status = "connected"
            self.error = ""
        except Exception as exc:  # pragma: no cover - depends on optional Redis
            self.client = None
            self.status = "unavailable"
            self.error = str(exc)

    async def publish(self, topic: str, payload: dict) -> bool:
        if not self.client:
            return False
        try:
            await self.client.publish(f"{self.settings.redis_namespace}:{topic}", json.dumps(payload, default=str))
            return True
        except Exception as exc:  # pragma: no cover - depends on optional Redis
            self.status = "error"
            self.error = str(exc)
            return False

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "status": self.status,
            "namespace": self.settings.redis_namespace,
            "error": self.error,
        }
