"""TTS на Piper (CPU): стриминг PCM-чанков, lazy-load голосов, fallback по языку."""
import asyncio
import json
import os
import threading

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI()

CONFIG_PATH = os.environ.get("CONFIG_FILE", "/etc/aitaxassistent/tts.yaml")
CFG: dict = {}
VOICES: dict = {}
LOCK = threading.Lock()


class SynthesizeRequest(BaseModel):
    session_id: str
    text: str
    language: str
    voice_id: str | None = None
    speed: float = 1.0


@app.on_event("startup")
def _startup():
    global CFG
    with open(CONFIG_PATH, encoding="utf-8") as f:
        CFG = yaml.safe_load(f)


def _load_voice(voice_id: str):
    with LOCK:
        if voice_id in VOICES:
            return VOICES[voice_id]
        v = CFG.get("voices", {}).get(voice_id)
        if not v:
            raise KeyError(f"voice {voice_id} not configured")
        from piper import Piper

        voice = Piper.load(v["model"], v["config"])
        with open(v["config"], encoding="utf-8") as f:
            voice.sample_rate = json.load(f).get("sample_rate", 22050)
        VOICES[voice_id] = voice
        return voice


def _synthesize_iter(voice, text: str, speed: float):
    try:
        yield from voice.synthesize(
            text, length_scale=1.0 / max(0.5, min(2.0, speed))
        )
    except Exception:  # noqa: BLE001
        return


@app.get("/health")
def health():
    return {
        "status": "ok",
        "voices_loaded": sorted(VOICES.keys()),
        "voices_available": sorted(CFG.get("voices", {}).keys()),
    }


@app.post("/synthesize")
async def synthesize(req: SynthesizeRequest):
    voice_id = req.voice_id or CFG.get("defaults", {}).get(req.language)
    if not voice_id:
        raise HTTPException(400, "no voice configured for language")
    try:
        voice = _load_voice(voice_id)
    except Exception:  # noqa: BLE001
        fb = CFG.get("fallbacks", {}).get(req.language)
        if not fb or fb == voice_id:
            raise HTTPException(503, "tts unavailable")
        voice = _load_voice(fb)

    sample_rate = voice.sample_rate

    async def generate():
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def run():
            try:
                for chunk in _synthesize_iter(voice, req.text, req.speed):
                    loop.call_soon_threadsafe(q.put_nowait, chunk.tobytes())
            except Exception:  # noqa: BLE001
                pass
            loop.call_soon_threadsafe(q.put_nowait, None)

        threading.Thread(target=run, daemon=True).start()
        while True:
            item = await q.get()
            if item is None:
                break
            yield item

    return StreamingResponse(
        generate(),
        media_type="audio/pcm",
        headers={
            "X-Format": "s16le",
            "X-Sample-Rate": str(sample_rate),
            "X-Channels": "1",
            "X-Voice": voice_id,
        },
    )
