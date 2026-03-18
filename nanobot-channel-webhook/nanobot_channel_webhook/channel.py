import asyncio
from typing import Any

import aiohttp
from aiohttp import web
from loguru import logger
from pydantic import Field

from nanobot.channels.base import BaseChannel
from nanobot.bus.events import OutboundMessage
from nanobot.config.schema import Base


class WebhookConfig(Base):
    """Webhook channel configuration."""

    enabled: bool = False
    port: int = 9000
    timeout: int = 120
    allow_from: list[str] = Field(default_factory=list)


class WebhookChannel(BaseChannel):
    name = "webhook"
    display_name = "Webhook"

    def __init__(self, config: Any, bus: Any):
        if isinstance(config, dict):
            config = WebhookConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebhookConfig = config
        # chat_id -> callback_url (for async callback mode)
        self._callbacks: dict[str, str] = {}
        # chat_id -> asyncio.Future (for sync wait mode)
        self._waiters: dict[str, asyncio.Future] = {}

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {"enabled": False, "port": 9000, "timeout": 120, "allowFrom": []}

    async def start(self) -> None:
        self._running = True
        port = self.config.port

        app = web.Application()
        app.router.add_post("/message", self._on_request)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info("Webhook listening on :{}", port)

        while self._running:
            await asyncio.sleep(1)

        await runner.cleanup()

    async def stop(self) -> None:
        self._running = False
        # Cancel all pending waiters
        for fut in self._waiters.values():
            if not fut.done():
                fut.cancel()

    async def send(self, msg: OutboundMessage) -> None:
        # Skip streaming progress chunks, only deliver final reply
        if msg.metadata.get("_progress"):
            return

        chat_id = msg.chat_id
        payload = {"chat_id": chat_id, "content": msg.content, "media": msg.media}

        # Mode 1: If a sync waiter is pending, resolve the Future
        fut = self._waiters.pop(chat_id, None)
        if fut and not fut.done():
            fut.set_result(payload)
            return

        # Mode 2: POST to callback_url
        callback_url = self._callbacks.pop(chat_id, None)
        if callback_url:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(callback_url, json=payload) as resp:
                        logger.info(
                            "[webhook] callback -> {} status={}", callback_url, resp.status
                        )
            except Exception as e:
                logger.error("[webhook] callback failed: {}", e)
            return

        logger.warning("[webhook] no callback/waiter for chat_id={}", chat_id)

    async def _on_request(self, request: web.Request) -> web.Response:
        body = await request.json()
        sender = body.get("sender", "unknown")
        chat_id = body.get("chat_id", sender)
        text = body.get("text", "")
        media = body.get("media", [])
        callback_url = body.get("callback_url")
        sync = body.get("sync", False)

        if not text and not media:
            return web.json_response({"ok": False, "error": "text or media required"}, status=400)

        # Sync mode: wait for agent reply within the same HTTP request
        if sync:
            timeout = self.config.timeout
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self._waiters[chat_id] = fut

            await self._handle_message(
                sender_id=sender, chat_id=chat_id, content=text, media=media,
            )

            try:
                result = await asyncio.wait_for(fut, timeout=timeout)
                return web.json_response({"ok": True, **result})
            except asyncio.TimeoutError:
                self._waiters.pop(chat_id, None)
                return web.json_response(
                    {"ok": False, "error": "timeout waiting for agent reply"}, status=504
                )

        # Async mode: store callback_url (if provided), return immediately
        if callback_url:
            self._callbacks[chat_id] = callback_url

        await self._handle_message(
            sender_id=sender, chat_id=chat_id, content=text, media=media,
        )

        return web.json_response({"ok": True})
