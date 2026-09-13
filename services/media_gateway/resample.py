import soxr


def resample(audio, from_sr: int, to_sr: int):
    """float32 [-1, 1] -> float32 [-1, 1]."""
    if from_sr == to_sr:
        return audio
    return soxr.resample(audio, from_sr, to_sr)
