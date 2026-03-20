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
        logger.debug("[webhook] initializing with raw config: {}", config)
        if isinstance(config, dict):
            config = WebhookConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebhookConfig = config
        self._callback_urls: dict[str, str] = {}  # chat_id -> callback_url
        self._session: aiohttp.ClientSession | None = None
        logger.debug("[webhook] initialized, port={}, allow_from={}", config.port, config.allow_from)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {"enabled": False, "port": 9000, "allowFrom": []}

    async def start(self) -> None:
        logger.debug("[webhook] starting channel...")
        self._running = True
        self._session = aiohttp.ClientSession()
        logger.debug("[webhook] aiohttp ClientSession created")

        app = web.Application()
        app.router.add_post("/message", self._on_request)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.config.port)
        await site.start()
        logger.info("[webhook] listening on 0.0.0.0:{}", self.config.port)

        while self._running:
            await asyncio.sleep(1)

        logger.debug("[webhook] event loop exiting, cleaning up...")
        await runner.cleanup()
        logger.debug("[webhook] runner cleaned up")
        if self._session:
            await self._session.close()
            logger.debug("[webhook] client session closed")

    async def stop(self) -> None:
        logger.info("[webhook] stopping channel...")
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        logger.debug("[webhook] send() called, msg={}", msg)
        callback_url = self._callback_urls.get(msg.chat_id)
        if not callback_url or not self._session:
            logger.warning("[webhook] no callback_url for chat_id={}, dropping reply "
                           "(session_exists={}, known_chat_ids={})",
                           msg.chat_id, self._session is not None, list(self._callback_urls.keys()))
            return

        payload = {
            "chat_id": msg.chat_id,
            "content": msg.content or "",
            "media": msg.media or [],
        }
        logger.debug("[webhook] posting to callback_url={}, payload_keys={}", callback_url, list(payload.keys()))

        try:
            async with self._session.post(callback_url, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning("[webhook] callback failed ({}): {}", resp.status, body[:200])
                else:
                    logger.debug("[webhook] callback succeeded, status={}", resp.status)
        except Exception as e:
            logger.warning("[webhook] callback error for chat_id={}: {} ({})", msg.chat_id, type(e).__name__, e)

    async def _on_request(self, request: web.Request) -> web.Response:
        remote = request.remote
        logger.debug("[webhook] incoming request from {}, method={}, path={}",
                      remote, request.method, request.path)

        try:
            body = await request.json()
        except Exception as e:
            logger.warning("[webhook] invalid JSON from {}: {}", remote, e)
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        logger.debug("[webhook] request body keys={}", list(body.keys()))

        sender = body.get("sender", "unknown")
        chat_id = body.get("chat_id", sender)
        text = body.get("text", "")
        media = body.get("media", [])
        callback_url = body.get("callback_url")

        logger.debug("[webhook] parsed: sender={}, chat_id={}, text_len={}, media_count={}, callback_url={}",
                      sender, chat_id, len(text), len(media), callback_url)

        if callback_url:
            self._callback_urls[chat_id] = callback_url
            logger.debug("[webhook] stored callback_url for chat_id={}", chat_id)

        logger.debug("[webhook] dispatching to _handle_message, chat_id={}", chat_id)
        await self._handle_message(
            sender_id=sender,
            chat_id=chat_id,
            content=text,
            media=media,
        )
        logger.debug("[webhook] _handle_message completed for chat_id={}", chat_id)

        return web.json_response({"ok": True})
