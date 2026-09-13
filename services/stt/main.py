import base64
import os

import numpy as np
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

CONFIG_PATH = os.environ.get("CONFIG_FILE", "/etc/caller/stt.yaml")
engine = None


class TranscribeRequest(BaseModel):
    session_id: str
    turn_id: int
    audio_b64: str
    sample_rate: int = 16000
    language_hint: str | None = None


@app.on_event("startup")
def _startup():
    global engine
    from engine import SttEngine

    engine = SttEngine(CONFIG_PATH)


@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": engine is not None and engine.ready}


@app.post("/transcribe")
def transcribe(req: TranscribeRequest):
    if engine is None or not engine.ready:
        raise HTTPException(503, "STT model not loaded")
    try:
        raw = base64.b64decode(req.audio_b64)
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception:
        raise HTTPException(400, "invalid audio payload")
    try:
        out = engine.transcribe(audio, req.sample_rate)
        out["session_id"] = req.session_id
        out["turn_id"] = req.turn_id
        return out
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"transcription failed: {e}")
