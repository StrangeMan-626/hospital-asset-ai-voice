import asyncio
from dashscope.audio.tts_v2 import SpeechSynthesizer
from app.core.config import get_settings
from app.core.dashscope_config import configure_dashscope
from app.core.errors import RelayError


async def synthesize(text: str) -> bytes:
    settings = get_settings()
    configure_dashscope(settings)

    def _call():
        synthesizer = SpeechSynthesizer(model=settings.TTS_MODEL, voice=settings.TTS_VOICE)
        audio = synthesizer.call(text)
        return audio

    try:
        audio = await asyncio.wait_for(
            asyncio.to_thread(_call),
            timeout=settings.TTS_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise RelayError(504, "TTS upstream timeout")
    except Exception as e:
        raise RelayError(500, f"TTS SDK error: {e}")

    if not audio:
        raise RelayError(500, "TTS returned empty audio")

    return audio
