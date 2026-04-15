import asyncio

from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

from app.core.config import get_settings
from app.core.dashscope_config import configure_dashscope
from app.core.errors import ErrorCode, RelayError


def get_tts_audio_format(audio_format: str) -> AudioFormat:
    fmt = (audio_format or "mp3").lower()
    if fmt == "wav":
        return AudioFormat.WAV_16000HZ_MONO_16BIT
    if fmt == "pcm":
        return AudioFormat.PCM_16000HZ_MONO_16BIT
    if fmt == "opus":
        return AudioFormat.OGG_OPUS_24KHZ_MONO_32KBPS
    return AudioFormat.DEFAULT


def get_tts_media_type(audio_format: str) -> str:
    fmt = (audio_format or "mp3").lower()
    if fmt == "wav":
        return "audio/wav"
    if fmt == "pcm":
        return "audio/pcm"
    if fmt == "opus":
        return "audio/ogg"
    return "audio/mpeg"


def resolve_codec_info(audio_format: str) -> tuple[str, int]:
    fmt = (audio_format or "mp3").lower()
    if fmt == "pcm":
        return "pcm_s16le", 16000
    if fmt == "wav":
        return "wav", 16000
    return "audio/mpeg", 24000


async def synthesize(text: str) -> bytes:
    settings = get_settings()
    configure_dashscope(settings)

    def _call():
        synthesizer = SpeechSynthesizer(
            model=settings.TTS_SYNC_MODEL,
            voice=settings.TTS_VOICE,
            format=get_tts_audio_format(settings.TTS_AUDIO_FORMAT),
        )
        audio = synthesizer.call(text, timeout_millis=settings.TTS_TIMEOUT * 1000)
        return audio

    try:
        audio = await asyncio.wait_for(
            asyncio.to_thread(_call),
            timeout=settings.TTS_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise RelayError(504, "TTS upstream timeout", ErrorCode.TTS_TIMEOUT)
    except Exception as e:
        raise RelayError(500, f"TTS SDK error: {e}", ErrorCode.TTS_FAIL)

    if not audio:
        raise RelayError(500, "TTS returned empty audio", ErrorCode.TTS_FAIL)

    return audio
