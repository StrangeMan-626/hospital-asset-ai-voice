import asyncio
import logging
import time

from dashscope.audio.tts_v2 import ResultCallback, SpeechSynthesizer
from fastapi import WebSocket

from app.core.config import Settings
from app.core.dashscope_config import configure_dashscope
from app.core.errors import ErrorCode, RelayError, ws_error_msg
from app.core.logger import logging_context
from app.core.metrics import metrics
from app.models.session import Session
from app.services.tts_service import get_tts_audio_format

logger = logging.getLogger("voice-relay")


class _TTSBridge(ResultCallback):
    def __init__(self, owner: "TTSRealtimeSession"):
        self._owner = owner

    def on_open(self) -> None:
        return

    def on_complete(self) -> None:
        if self._owner.loop is not None:
            self._owner.loop.call_soon_threadsafe(self._owner.complete_event.set)

    def on_error(self, message) -> None:
        if self._owner.loop is not None:
            asyncio.run_coroutine_threadsafe(self._owner.handle_error(message), self._owner.loop)

    def on_close(self) -> None:
        return

    def on_event(self, message: str) -> None:
        if self._owner.loop is not None:
            asyncio.run_coroutine_threadsafe(self._owner.handle_event(message), self._owner.loop)

    def on_data(self, data: bytes) -> None:
        if self._owner.loop is not None:
            asyncio.run_coroutine_threadsafe(self._owner.handle_audio_chunk(data), self._owner.loop)


class TTSRealtimeSession:
    def __init__(self, session: Session, ws: WebSocket, settings: Settings):
        self._session = session
        self._ws = ws
        self._settings = settings
        self._synthesizer: SpeechSynthesizer | None = None
        self._bridge = _TTSBridge(self)
        self._first_chunk_sent = False
        self._cancelled = False
        self._closed = False
        self._error: RelayError | None = None
        self._start_time = 0.0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.complete_event = asyncio.Event()
        self.first_chunk_event = asyncio.Event()

    async def start(self) -> None:
        configure_dashscope(self._settings)
        self.loop = asyncio.get_running_loop()
        self.complete_event.clear()
        self.first_chunk_event.clear()
        self._error = None
        self._first_chunk_sent = False
        self._cancelled = False
        self._synthesizer = SpeechSynthesizer(
            model=self._settings.TTS_REALTIME_MODEL,
            voice=self._settings.TTS_VOICE,
            format=get_tts_audio_format(self._settings.TTS_AUDIO_FORMAT),
            callback=self._bridge,
        )

    async def send_text(self, text: str) -> None:
        if self._closed or self._synthesizer is None:
            raise RelayError(400, "TTS session is closed", ErrorCode.TTS_FAIL)
        self._session.touch()
        if self._start_time == 0.0:
            self._start_time = time.perf_counter()
        try:
            await asyncio.to_thread(self._synthesizer.streaming_call, text)
        except TimeoutError as exc:
            raise RelayError(504, "TTS connect timeout", ErrorCode.TTS_TIMEOUT) from exc
        except Exception as exc:
            raise RelayError(502, f"TTS realtime send failed: {exc}", ErrorCode.TTS_CONNECT_FAIL) from exc
        if not self._first_chunk_sent:
            try:
                await asyncio.wait_for(
                    self.first_chunk_event.wait(),
                    timeout=self._settings.TTS_FIRST_CHUNK_TIMEOUT,
                )
            except asyncio.TimeoutError as exc:
                raise RelayError(504, "TTS first chunk timeout", ErrorCode.TTS_TIMEOUT) from exc
        if self._error is not None:
            raise self._error

    async def finish(self) -> None:
        if self._closed or self._synthesizer is None:
            return
        try:
            await asyncio.to_thread(self._synthesizer.async_streaming_complete, self._settings.TTS_TIMEOUT * 1000)
            await asyncio.wait_for(self.complete_event.wait(), timeout=self._settings.TTS_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise RelayError(504, "TTS finish timeout", ErrorCode.TTS_TIMEOUT) from exc
        if self._error is not None:
            raise self._error
        if self._start_time:
            metrics.record("tts_total_ms", (time.perf_counter() - self._start_time) * 1000)

    async def cancel(self) -> None:
        self._cancelled = True
        if self._closed or self._synthesizer is None:
            return
        try:
            await asyncio.to_thread(self._synthesizer.streaming_cancel)
        except Exception:
            logger.debug("ignore TTS streaming cancel failure", exc_info=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._synthesizer is not None:
            try:
                await asyncio.to_thread(self._synthesizer.close)
            except Exception:
                logger.debug("ignore TTS close failure", exc_info=True)
            self._synthesizer = None

    async def handle_event(self, message: str) -> None:
        with logging_context(
            trace_id=self._session.trace_id,
            session_id=self._session.session_id,
            terminal_id=self._session.terminal_id,
        ):
            logger.debug("TTS realtime event: %s", message)

    async def handle_error(self, message: str) -> None:
        self._error = RelayError(502, "TTS realtime error", ErrorCode.TTS_FAIL)
        await self._ws.send_text(
            ws_error_msg(
                ErrorCode.TTS_FAIL,
                message if isinstance(message, str) else "TTS realtime error",
                session_id=self._session.session_id,
                trace_id=self._session.trace_id,
            )
        )

    async def handle_audio_chunk(self, data: bytes) -> None:
        if self._cancelled:
            return
        if not self._first_chunk_sent and self._start_time:
            metrics.record("tts_first_chunk_ms", (time.perf_counter() - self._start_time) * 1000)
            self._first_chunk_sent = True
            self.first_chunk_event.set()
        await self._ws.send_bytes(data)
