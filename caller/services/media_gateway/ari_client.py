import asyncio
import base64
import json
import logging

import aiohttp

log = logging.getLogger("ari")


class AriClient:
    """WS-клиент ARI (события) + REST для управления каналами."""

    def __init__(self, cfg: dict, on_event):
        self.url = cfg["url"].rstrip("/")
        self.app_name = cfg["app_name"]
        self.user = cfg.get("user")
        self.password = cfg.get("password")
        self.on_event = on_event
        self.session = None

    def _headers(self):
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    async def connect(self):
        self.session = aiohttp.ClientSession(headers=self._headers())
        await self._ws_loop()

    async def _ws_loop(self):
        backoff = 1
        while True:
            url = f"{self.url}/ari/events?app={self.app_name}"
            try:
                async with self.session.ws_connect(url) as ws:
                    backoff = 1
                    log.info("ARI WS connected")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            self.on_event(data.get("type", ""), data.get("data", {}))
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except Exception as e:  # noqa: BLE001
                log.warning("ARI WS error: %s", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def hold_channel(self, channel_id: str):
        r = await self.session.put(
            f"{self.url}/ari/channels/{channel_id}/actions", data={"hold": "true"}
        )
        r.read()

    async def close(self):
        if self.session:
            await self.session.close()
