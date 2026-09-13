import asyncio
import base64
import logging
import time

import aiohttp
import numpy as np

from resample import resample

log = logging.getLogger("session")


class AudioSession:
    """VAD/endpointing, STT-вызовы, barge-in, очередь воспроизведения TTS.

    Вход:  кадры L16 8kHz mono (20ms / 320 сэмплов) из ExternalMedia.
    Выход: события на orchestrator, аудиокадры в ExternalMedia.
    """

    def __init__(self, uniqueid, cfg, vad, orch_send):
        self.uniqueid = uniqueid
        self.cfg = cfg
        self.vad = vad
        self.orch_send = orch_send
        self.stt_url = cfg.get("stt", {}).get("url", "http://stt:8091").rstrip("/")
        self.tts_url = cfg.get("tts", {}).get("url", "http://tts:8095").rstrip("/")
        ep = cfg.get("endpointing", {})
        self.silence_s = ep.get("silence_after_speech_ms", 550) / 1000
        self.min_speech_s = ep.get("min_speech_ms", 300) / 1000
        self.max_utterance_s = ep.get("max_utterance_seconds", 30)
        bi = cfg.get("barge_in", {})
        self.barge_in_enabled = bi.get("enabled", True)
        self.min_interrupt_s = bi.get("min_interrupt_ms", 400) / 1000

        self.turn_id = 0
        self.started_at = time.time()
        self.ended = False
        self.ws = None

        # ingress
        self.speaking = False
        self.speech_started_at = 0.0
        self.last_speech_at = 0.0
        self.utterance = []
        self.preroll = []
        self.vad_buf = np.zeros(0, dtype=np.int16)
        self._stt_sem = asyncio.Semaphore(1)

        # egress
        self.play_queue = asyncio.Queue()
        self.playing = False
        self._barge_at = None

    async def feed_frame(self, frame: bytes):
        audio = np.frombuffer(frame, dtype=np.int16)
        if audio.size == 0:
            return
        now = time.monotonic()

        self.preroll.extend(audio.tolist())
        if len(self.preroll) > 6400:
            self.preroll = self.preroll[-4800:]

        self.vad_buf = np.concatenate([self.vad_buf, audio])
        while len(self.vad_buf) >= self.vad.frame_len:
            block = self.vad_buf[: self.vad.frame_len].copy()
            self.vad_buf = self.vad_buf[self.vad.frame_len :]
            prob = self.vad(block)
            self._vad_decision(prob, block, now)

    def _vad_decision(self, prob, block, now):
        if prob > 0.5:
            if not self.speaking:
                self.speaking = True
                self.speech_started_at = now
                self.last_speech_at = now
                preroll = np.array(self.preroll[-4800:], dtype=np.int16)
                self.utterance = [preroll, block.copy()]
                if self.barge_in_enabled and self.play_queue.qsize() > 0:
                    self._barge_at = now
            else:
                self.last_speech_at = now
                self.utterance.append(block)
                if self.barge_in_enabled and self._barge_at is not None:
                    if now - self._barge_at >= self.min_interrupt_s:
                        if self.play_queue.qsize() > 0:
                            self.clear_queue()
                            log.info("barge-in: TTS queue cleared")
                        self._barge_at = None
        else:
            if self.speaking:
                self.utterance.append(block)
                if now - self.speech_started_at > self.max_utterance_s:
                    self._finalize(now, force=True)
                elif now - self.last_speech_at >= self.silence_s:
                    self._finalize(now, force=False)

    def _finalize(self, now, force=False):
        self.speaking = False
        self._barge_at = None
        dur = now - self.speech_started_at
        if not force and dur < self.min_speech_s:
            self.utterance = []
            return
        if not self.utterance:
            return
        samples = np.concatenate(self.utterance)
        self.utterance = []
        max_samples = int(self.max_utterance_s * 8000)
        if len(samples) > max_samples:
            samples = samples[:max_samples]
        asyncio.get_running_loop().create_task(self._process_utterance(samples))

    async def _process_utterance(self, samples):
        async with self._stt_sem:
            self.turn_id += 1
            audio_16k = resample(
                samples.astype(np.float32) / 32768.0, 8000, 16000
            ) * 32768
            audio_16k = np.clip(audio_16k, -32768, 32767).astype(np.int16)
            try:
                async with aiohttp.ClientSession() as cs:
                    async with cs.post(
                        f"{self.stt_url}/transcribe",
                        json={
                            "session_id": self.uniqueid,
                            "turn_id": self.turn_id,
                            "audio_b64": base64.b64encode(audio_16k.tobytes()).decode(),
                            "sample_rate": 16000,
                        },
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as r:
                        if r.status != 200:
                            log.warning("STT http %s", r.status)
                            return
                        result = await r.json()
                await self.orch_send(
                    {
                        "event": "utterance_final",
                        "session_id": self.uniqueid,
                        "turn_id": self.turn_id,
                        "payload": result,
                    }
                )
            except Exception as e:  # noqa: BLE001
                log.warning("STT call failed: %s", e)

    def clear_queue(self):
        while True:
            try:
                self.play_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._barge_at = None

    async def speak(self, data: dict):
        """TTS-запрос по команде orchestrator: текст -> стрим PCM -> очередь кадров."""
        text = (data.get("text") or "").strip()
        if not text:
            return
        if data.get("interrupt", True):
            self.clear_queue()
        lang = data.get("lang", "ru")
        speed = float(data.get("speed", 1.0))
        try:
            async with aiohttp.ClientSession() as cs:
                async with cs.post(
                    f"{self.tts_url}/synthesize",
                    json={
                        "session_id": self.uniqueid,
                        "text": text,
                        "language": lang,
                        "voice_id": data.get("voice_id"),
                        "speed": speed,
                    },
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as r:
                    if r.status != 200:
                        log.warning("TTS http %s", r.status)
                        return
                    sr = int(r.headers.get("X-Sample-Rate", 22050))
                    buf = bytearray()
                    while True:
                        chunk = await r.content.readany()
                        if not chunk:
                            break
                        buf.extend(chunk)
                        if len(buf) >= sr * 2 * 3:
                            self._flush_tts_buf(buf, sr)
                    self._flush_tts_buf(buf, sr)
        except Exception as e:  # noqa: BLE001
            log.warning("TTS fetch failed: %s", e)

    def _flush_tts_buf(self, buf, sr):
        if not buf:
            return
        audio = np.frombuffer(bytes(buf), dtype=np.int16)
        buf.clear()
        if audio.size == 0:
            return
        out = resample(audio.astype(np.float32) / 32768.0, sr, 8000) * 32768
        out = np.clip(out, -32768, 32767).astype(np.int16)
        for i in range(0, len(out) - 319, 320):
            self.play_queue.put_nowait(out[i : i + 320].tobytes())
        self.playing = True

    async def run_play_loop(self):
        while not self.ended:
            ws = self.ws
            if ws is None or ws.closed:
                await asyncio.sleep(0.1)
                continue
            try:
                frame = await asyncio.wait_for(self.play_queue.get(), timeout=0.2)
                await ws.send_bytes(frame)
            except asyncio.TimeoutError:
                if self.play_queue.empty():
                    self.playing = False
            except Exception:  # noqa: BLE001
                self.clear_queue()
