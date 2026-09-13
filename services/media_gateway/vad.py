import numpy as np
import onnxruntime


class SileroVAD:
    """Silero VAD (onnx). 8 kHz, блоки по 512 сэмплов (100 мс)."""

    def __init__(self, model_path: str, sample_rate: int = 8000):
        self.sr = sample_rate
        self.sess = onnxruntime.InferenceSession(
            model_path, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.sess.get_inputs()[0].name
        self.frame_len = {8000: 512, 16000: 1024}.get(sample_rate, 512)
        self.reset()

    def reset(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)

    def __call__(self, audio: np.ndarray) -> float:
        a = audio.astype(np.float32) / 32768.0
        ort_inputs = {
            self.input_name: np.expand_dims(a, 0),
            "sr": np.array(self.sr, dtype=np.int64),
            "state": self.state,
            "return_state": np.array(True, dtype=bool),
        }
        out = self.sess.run(None, ort_inputs)
        self.state = out[1]
        return float(out[0][0][-1])
