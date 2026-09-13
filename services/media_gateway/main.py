"""Media Gateway: ExternalMedia WS (Asterisk) + ARI (события) + WS на orchestrator."""
import asyncio
import json
import logging
import time

import aiohttp
from aiohttp import web

import config_loader
from ari_client import AriClient
from session import AudioSession
from vad import SileroVAD

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mgw")

CFG = None
STATE = {
    "sessions": {},
    "ari": None,
    "orch_ws": None,
    "orch_lock": None,
    "vad": None,
}


async def _send_orch(payload: dict):
    ws = STATE["orch_ws"]
    if ws is None or ws.closed:
        log.warning("orch WS not connected, dropping: %s", payload.get("event"))
        return
    async with STATE["orch_lock"]:
        try:
            await ws.send_json(payload)
        except Exception as e:  # noqa: BLE001
            log.warning("send to orchestrator failed: %s", e)


def _handle_ari_event(etype, data):
    loop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if etype == "ChannelStateChange":
        ch = data.get("channel", {})
        uid = ch.get("id", "")
        state = data.get("state", "")
        if uid in STATE["sessions"] and state in ("Down", "Hungup"):
            loop.create_task(_end_session(uid, "hangup"))
    elif etype == "ChannelDtmf":
        ch = data.get("channel", {})
        uid = ch.get("id", "")
        if uid in STATE["sessions"]:
            loop.create_task(
                _send_orch(
                    {
                        "event": "dtmf",
                        "session_id": uid,
                        "payload": {
                            "digit": data.get("digit", ""),
                            "ts": time.time(),
                        },
                    }
                )
            )


async def _end_session(uniqueid, reason, close_ws=True):
    session = STATE["sessions"].pop(uniqueid, None)
    if not session or session.ended:
        return
    session.ended = True
    await _send_orch(
        {
            "event": "call_ended",
            "session_id": uniqueid,
            "payload": {"reason": reason},
        }
    )
    if close_ws and session.ws and not session.ws.closed:
        await session.ws.close()


# ---------- ExternalMedia (Asterisk -> мы) ----------


async def stream_handler(request):
    uniqueid = request.match_info["uniqueid"]
    ws = web.WebSocketResponse(max_msg_size=2**20)
    await ws.prepare(request)
    log.info("ExternalMedia connected: %s", uniqueid)

    session = AudioSession(uniqueid, CFG, STATE["vad"], _send_orch)
    STATE["sessions"][uniqueid] = session
    session.ws = ws
    await _send_orch(
        {
            "event": "call_started",
            "session_id": uniqueid,
            "payload": {
                "caller": request.query.get("caller", ""),
                "uniqueid": uniqueid,
                "codec": "L16-8k",
            },
        }
    )
    loop = asyncio.get_running_loop()
    loop.create_task(session.run_play_loop())

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                await session.feed_frame(msg.data)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                if msg.data.strip() == "hb":
                    await ws.send_str("hb")
            elif msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                break
    finally:
        await _end_session(uniqueid, "ws_closed")
    return ws


# ---------- Orchestrator (мы <-> оркестратор) ----------


async def handle_command(cmd: dict):
    session = STATE["sessions"].get(cmd.get("session_id", ""))
    if not session:
        log.warning("command for unknown session: %s", cmd.get("session_id"))
        return
    c = cmd.get("command")
    data = cmd.get("data") or {}
    if c == "speak":
        asyncio.get_running_loop().create_task(session.speak(data))
    elif c == "clear_queue":
        session.clear_queue()
    elif c == "hangup":
        await _end_session(cmd["session_id"], "hangup")
    elif c == "transfer":
        # Transfer = закрытие ExternalMedia WS: dialplan Asterisk сам
        # маршрутизирует звонок на очередь операторов (failover-ветка).
        log.info("transfer session %s reason=%s", cmd["session_id"], data.get("reason"))
        session.clear_queue()
        if session.ws and not session.ws.closed:
            await session.ws.close()


async def orchestrator_loop():
    url = CFG["orchestrator"]["ws_url"]
    backoff = 1
    while True:
        try:
            async with aiohttp.ClientSession() as cs:
                async with cs.ws_connect(url) as ws:
                    STATE["orch_ws"] = ws
                    backoff = 1
                    log.info("orchestrator WS connected")
                    for uid in list(STATE["sessions"].keys()):
                        await _send_orch(
                            {
                                "event": "gateway_reconnected",
                                "session_id": uid,
                                "payload": {},
                            }
                        )
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await handle_command(json.loads(msg.data))
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
        except Exception as e:  # noqa: BLE001
            log.warning("orchestrator WS error: %s", e)
        STATE["orch_ws"] = None
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


# ---------- App ----------


async def on_startup(app):
    global CFG
    CFG = config_loader.load()
    STATE["orch_lock"] = asyncio.Lock()
    STATE["vad"] = SileroVAD(
        CFG.get("models", {}).get("silero_vad", "models/silero_vad.onnx"), 8000
    )
    STATE["ari"] = AriClient(CFG["ari"], on_event=_handle_ari_event)
    loop = asyncio.get_running_loop()
    loop.create_task(STATE["ari"].connect())
    loop.create_task(orchestrator_loop())


async def on_shutdown(app):
    if STATE["ari"]:
        await STATE["ari"].close()


def create_app():
    app = web.Application()
    app.router.add_get("/stream/{uniqueid}", stream_handler)
    app.router.add_get("/health", health)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


async def health(request):
    return web.json_response(
        {
            "status": "ok",
            "sessions": len(STATE["sessions"]),
            "orch_connected": STATE["orch_ws"] is not None
            and not STATE["orch_ws"].closed,
        }
    )


app = create_app()

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8081)
