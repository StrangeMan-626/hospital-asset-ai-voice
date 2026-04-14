import sherpa_onnx
import numpy as np
from app.core.config import get_settings


def create_kws() -> sherpa_onnx.KeywordSpotter:
    settings = get_settings()
    model_dir = settings.WAKEUP_MODEL_DIR
    return sherpa_onnx.KeywordSpotter(
        encoder=f"{model_dir}/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        decoder=f"{model_dir}/decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        joiner=f"{model_dir}/joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        tokens=f"{model_dir}/tokens.txt",
        keywords_file=settings.WAKEUP_KEYWORDS_FILE,
        num_threads=settings.WAKEUP_NUM_THREADS,
        keywords_threshold=settings.WAKEUP_KEYWORDS_THRESHOLD,
        keywords_score=settings.WAKEUP_KEYWORDS_SCORE,
    )


_kws: sherpa_onnx.KeywordSpotter | None = None


def get_kws() -> sherpa_onnx.KeywordSpotter:
    global _kws
    if _kws is None:
        _kws = create_kws()
    return _kws


def decode_pcm(pcm_bytes: bytes) -> np.ndarray:
    return np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
