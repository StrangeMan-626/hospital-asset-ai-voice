import logging
import time

from fastapi import WebSocket

from app.services import asr_service, tts_service

logger = logging.getLogger("voice-relay")


class DegradeService:
    def __init__(self, settings):
        self._asr_fail_count = 0
        self._tts_fail_count = 0
        self._asr_circuit_open = False
        self._tts_circuit_open = False
        self._asr_open_time = 0.0
        self._tts_open_time = 0.0
        self._asr_half_open_used = False
        self._tts_half_open_used = False
        self._threshold = 5
        self._circuit_timeout_s = max(30, settings.DEGRADE_RECOVER_INTERVAL_MS / 1000)
        self._enabled = settings.DEGRADE_ENABLED

    def _record_fail(self, kind: str) -> None:
        if not self._enabled:
            return
        if kind == "asr":
            self._asr_fail_count += 1
            if self._asr_fail_count >= self._threshold or self._asr_half_open_used:
                self._asr_circuit_open = True
                self._asr_open_time = time.monotonic()
                self._asr_half_open_used = False
        else:
            self._tts_fail_count += 1
            if self._tts_fail_count >= self._threshold or self._tts_half_open_used:
                self._tts_circuit_open = True
                self._tts_open_time = time.monotonic()
                self._tts_half_open_used = False

    def _record_success(self, kind: str) -> None:
        if not self._enabled:
            return
        if kind == "asr":
            self._asr_fail_count = 0
            self._asr_circuit_open = False
            self._asr_open_time = 0.0
            self._asr_half_open_used = False
        else:
            self._tts_fail_count = 0
            self._tts_circuit_open = False
            self._tts_open_time = 0.0
            self._tts_half_open_used = False

    def record_asr_fail(self) -> None:
        self._record_fail("asr")

    def record_asr_success(self) -> None:
        self._record_success("asr")

    def record_tts_fail(self) -> None:
        self._record_fail("tts")

    def record_tts_success(self) -> None:
        self._record_success("tts")

    def _should_degrade(self, kind: str) -> bool:
        if not self._enabled:
            return False
        if kind == "asr":
            if self._asr_circuit_open:
                if time.monotonic() - self._asr_open_time >= self._circuit_timeout_s:
                    if self._asr_half_open_used:
                        return True
                    self._asr_circuit_open = False
                    self._asr_half_open_used = True
                    return False
                return True
            return False
        if self._tts_circuit_open:
            if time.monotonic() - self._tts_open_time >= self._circuit_timeout_s:
                if self._tts_half_open_used:
                    return True
                self._tts_circuit_open = False
                self._tts_half_open_used = True
                return False
            return True
        return False

    def should_degrade_asr(self) -> bool:
        return self._should_degrade("asr")

    def should_degrade_tts(self) -> bool:
        return self._should_degrade("tts")

    async def asr_sync_fallback(self, audio_buffer: bytes, filename: str = "audio.pcm") -> dict:
        logger.warning("ASR degraded to sync mode")
        result = await asr_service.recognize(audio_buffer, filename=filename)
        result["degraded"] = True
        return result

    async def tts_sync_fallback(
        self,
        text: str,
        ws: WebSocket,
        session_id: str = "",
        trace_id: str = "",
    ) -> bytes:
        logger.warning("TTS degraded to sync mode for session %s trace %s", session_id, trace_id)
        audio = await tts_service.synthesize(text)
        for idx in range(0, len(audio), 4096):
            await ws.send_bytes(audio[idx : idx + 4096])
        return audio
