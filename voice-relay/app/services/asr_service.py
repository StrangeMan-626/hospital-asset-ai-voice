import asyncio
import logging
import os
import subprocess
import tempfile

from dashscope.audio.asr import Recognition

from app.core.config import get_settings
from app.core.dashscope_config import configure_dashscope
from app.core.errors import ErrorCode, RelayError

logger = logging.getLogger("voice-relay")

NATIVE_FORMATS = {"wav", "mp3", "pcm"}


def _convert_to_wav(src_path: str) -> str:
    dst_path = src_path.rsplit(".", 1)[0] + ".wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", src_path, "-ar", "16000", "-ac", "1", "-f", "wav", dst_path],
        capture_output=True, check=True,
    )
    return dst_path


async def recognize(audio_bytes: bytes, filename: str = "audio.wav") -> dict:
    settings = get_settings()
    configure_dashscope(settings)

    ext = os.path.splitext(filename)[1] or ".wav"
    fmt = ext.lstrip(".").lower()

    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    wav_path = None
    try:
        tmp.write(audio_bytes)
        tmp.close()

        if fmt not in NATIVE_FORMATS:
            wav_path = await asyncio.to_thread(_convert_to_wav, tmp.name)
            audio_path = wav_path
            fmt = "wav"
        else:
            audio_path = tmp.name

        def _call():
            recognition = Recognition(
                model=settings.ASR_SYNC_MODEL,
                format=fmt,
                sample_rate=16000,
                language_hints=["zh", "en"],
                callback=None,
            )
            return recognition.call(audio_path)

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_call),
                timeout=settings.ASR_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise RelayError(504, "ASR upstream timeout", ErrorCode.ASR_TIMEOUT)
        except Exception as e:
            raise RelayError(502, f"ASR SDK error: {e}", ErrorCode.ASR_CONNECT_FAIL)

        logger.info("ASR raw output: %s", result)
        sentences = result.get_sentence()
        if not sentences:
            return {"text": "", "language": "zh", "duration": 0}

        text = "".join(s["text"] for s in sentences if "text" in s)
        return {"text": text, "language": "zh", "duration": 0}
    finally:
        os.unlink(tmp.name)
        if wav_path and os.path.exists(wav_path):
            os.unlink(wav_path)
