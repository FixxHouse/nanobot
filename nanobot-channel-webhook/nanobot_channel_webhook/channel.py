"""Webhook channel plugin for nanobot."""

import asyncio
from typing import Any

import aiohttp
from aiohttp import web
from loguru import logger
from nanobot.channels.base import BaseChannel
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Base


class WebhookConfig(Base):
    enabled: bool = False
    port: int = 9000
    allow_from: list[str] = []


class WebhookChannel(BaseChannel):
    name = "webhook"
    display_name = "Webhook"

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = WebhookConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebhookConfig = config
        self._callback_urls: dict[str, str] = {}  # chat_id -> callback_url
        self._session: aiohttp.ClientSession | None = None

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {"enabled": False, "port": 9000, "allowFrom": []}

    async def start(self) -> None:
        self._running = True
        self._session = aiohttp.ClientSession()

        app = web.Application()
        app.router.add_post("/message", self._on_request)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.config.port)
        await site.start()
        logger.info("Webhook listening on :{}", self.config.port)

        while self._running:
            await asyncio.sleep(1)

        await runner.cleanup()
        if self._session:
            await self._session.close()

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        callback_url = self._callback_urls.get(msg.chat_id)
        if not callback_url or not self._session:
            logger.warning("[webhook] no callback_url for chat_id={}, dropping reply", msg.chat_id)
            return

        payload = {
            "chat_id": msg.chat_id,
            "content": msg.content or "",
            "media": msg.media or [],
        }

        try:
            async with self._session.post(callback_url, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning("[webhook] callback failed ({}): {}", resp.status, body[:200])
        except Exception as e:
            logger.warning("[webhook] callback error for {}: {}", msg.chat_id, e)

    async def _on_request(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        sender = body.get("sender", "unknown")
        chat_id = body.get("chat_id", sender)
        text = body.get("text", "")
        media = body.get("media", [])
        callback_url = body.get("callback_url")

        if callback_url:
            self._callback_urls[chat_id] = callback_url

        await self._handle_message(
            sender_id=sender,
            chat_id=chat_id,
            content=text,
            media=media,
        )

        return web.json_response({"ok": True})
