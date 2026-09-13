import math
import os

import yaml


class SttEngine:
    """faster-whisper: Whisper large-v3-turbo, файнтюн kz/ru, телефонное качество."""

    def __init__(self, config_path: str):
        with open(config_path, encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f)
        self.ready = False
        self._load()

    def _load(self):
        from faster_whisper import WhisperModel

        m = self.cfg.get("model", {})
        self.model = WhisperModel(
            m.get("path", "/models/whisper/whisper-large-v3-turbo-FT-kzru"),
            device=m.get("device", "cuda"),
            compute_type=m.get("compute_type", "int8_float16"),
        )
        self.beam_size = int(m.get("beam_size", 5))
        self.vad_filter = bool(m.get("vad_filter", True))
        self.condition_prev = bool(m.get("condition_on_previous_text", False))
        self.model_name = os.path.basename(m.get("path", "whisper-ft"))
        self.ready = True

    def transcribe(self, audio, sample_rate: int) -> dict:
        segments, info = self.model.transcribe(
            audio,
            beam_size=self.beam_size,
            language=None,
            vad_filter=self.vad_filter,
            word_timestamps=True,
            condition_on_previous_text=self.condition_prev,
        )
        segs = list(segments)
        if not segs:
            return {
                "language": "unknown",
                "language_confidence": 0.0,
                "text": "",
                "confidence": 0.0,
                "no_speech_prob": round(float(info.no_speech_prob or 0.0), 4),
                "words": [],
                "duration_ms": int((info.duration or 0) * 1000),
                "model": self.model_name,
            }

        confs = [math.exp(s.avg_logprob) for s in segs if s.avg_logprob is not None]
        nsp = [s.no_speech_prob for s in segs if s.no_speech_prob is not None]
        words = []
        for s in segs:
            for w in s.words or []:
                conf = (
                    round(max(0.0, min(1.0, math.exp(w.avg_logprob))), 4)
                    if w.avg_logprob is not None
                    else 0.0
                )
                words.append(
                    {"w": w.word, "c": conf, "t0": round(w.start, 3), "t1": round(w.end, 3)}
                )

        lang = info.language or "unknown"
        return {
            "language": lang if lang in ("ru", "kk") else "unknown",
            "language_confidence": round(float(info.language_probability or 0.0), 4),
            "text": " ".join(s.text.strip() for s in segs).strip(),
            "confidence": round(sum(confs) / len(confs), 4) if confs else 0.0,
            "no_speech_prob": round(max(nsp), 4) if nsp else 0.0,
            "words": words,
            "duration_ms": int((info.duration or 0) * 1000),
            "model": self.model_name,
        }
