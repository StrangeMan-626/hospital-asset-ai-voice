import asyncio
import json
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
        if self._owner.loop is not None:
            asyncio.run_coroutine_threadsafe(self._owner.handle_open(), self._owner.loop)

    def on_complete(self) -> None:
        if self._owner.loop is not None:
            self._owner.loop.call_soon_threadsafe(self._owner.complete_event.set)
            asyncio.run_coroutine_threadsafe(self._owner.handle_complete(), self._owner.loop)

    def on_error(self, message) -> None:
        if self._owner.loop is not None:
            asyncio.run_coroutine_threadsafe(self._owner.handle_error(message), self._owner.loop)

    def on_close(self) -> None:
        if self._owner.loop is not None:
            asyncio.run_coroutine_threadsafe(self._owner.handle_close(), self._owner.loop)

    def on_event(self, message: str) -> None:
        if self._owner.loop is not None:
            asyncio.run_coroutine_threadsafe(self._owner.handle_event(message), self._owner.loop)

    def on_data(self, data: bytes) -> None:
        if self._owner.loop is not None:
            asyncio.run_coroutine_threadsafe(self._owner.handle_audio_chunk(data), self._owner.loop)


def _resolve_codec_info(fmt: str) -> tuple[str, int]:
    """Returns (codec, sampleRate) based on TTS_AUDIO_FORMAT."""
    if fmt == "pcm":
        return "pcm_s16le", 16000
    if fmt == "wav":
        return "wav", 16000
    return "audio/mpeg", 24000


class TTSRealtimeSession:
    def __init__(self, session: Session, ws: WebSocket, settings: Settings):
        self._session = session
        self._ws = ws
        self._settings = settings
        self._synthesizer: SpeechSynthesizer | None = None
        self._bridge = _TTSBridge(self)
        self._first_chunk_sent = False
        self._tts_start_sent = False
        self._cancelled = False
        self._closed = False
        self._error: RelayError | None = None
        self._start_time = 0.0
        self._text_chunks = 0
        self._audio_chunks = 0
        self._audio_bytes = 0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.complete_event = asyncio.Event()
        self.first_chunk_event = asyncio.Event()

    @property
    def text_chunks(self) -> int:
        return self._text_chunks

    async def start(self) -> None:
        configure_dashscope(self._settings)
        self.loop = asyncio.get_running_loop()
        self.complete_event.clear()
        self.first_chunk_event.clear()
        self._error = None
        self._first_chunk_sent = False
        self._tts_start_sent = False
        self._cancelled = False
        self._text_chunks = 0
        self._audio_chunks = 0
        self._audio_bytes = 0
        self._synthesizer = SpeechSynthesizer(
            model=self._settings.TTS_REALTIME_MODEL,
            voice=self._settings.TTS_VOICE,
            format=get_tts_audio_format(self._settings.TTS_AUDIO_FORMAT),
            callback=self._bridge,
        )
        with logging_context(
            trace_id=self._session.trace_id,
            session_id=self._session.session_id,
            terminal_id=self._session.terminal_id,
        ):
            logger.info(
                "TTS realtime downstream init model=%s voice=%s format=%s timeout=%ss firstChunkTimeout=%ss",
                self._settings.TTS_REALTIME_MODEL,
                self._settings.TTS_VOICE,
                self._settings.TTS_AUDIO_FORMAT,
                self._settings.TTS_TIMEOUT,
                self._settings.TTS_FIRST_CHUNK_TIMEOUT,
            )

    async def send_text(self, text: str) -> None:
        if self._closed or self._synthesizer is None:
            raise RelayError(400, "TTS session is closed", ErrorCode.TTS_FAIL)
        if not text.strip():
            raise RelayError(400, "TTS text content is empty", ErrorCode.TTS_FAIL)
        self._session.touch()
        if self._start_time == 0.0:
            self._start_time = time.perf_counter()
        self._text_chunks += 1
        with logging_context(
            trace_id=self._session.trace_id,
            session_id=self._session.session_id,
            terminal_id=self._session.terminal_id,
        ):
            logger.info(
                "TTS realtime send_text chunk=%s chars=%s preview=%r",
                self._text_chunks,
                len(text),
                text[:40],
            )
        try:
            await asyncio.to_thread(self._synthesizer.streaming_call, text)
        except TimeoutError as exc:
            raise RelayError(504, "TTS connect timeout", ErrorCode.TTS_TIMEOUT) from exc
        except Exception as exc:
            raise RelayError(502, f"TTS realtime send failed: {exc}", ErrorCode.TTS_CONNECT_FAIL) from exc
        if not self._first_chunk_sent:
            try:
                with logging_context(
                    trace_id=self._session.trace_id,
                    session_id=self._session.session_id,
                    terminal_id=self._session.terminal_id,
                ):
                    logger.info("TTS realtime waiting first audio chunk timeout=%ss", self._settings.TTS_FIRST_CHUNK_TIMEOUT)
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
        if self._text_chunks == 0:
            raise RelayError(400, "TTS finish called before any valid text", ErrorCode.TTS_FAIL)
        try:
            with logging_context(
                trace_id=self._session.trace_id,
                session_id=self._session.session_id,
                terminal_id=self._session.terminal_id,
            ):
                logger.info(
                    "TTS realtime finish requested textChunks=%s firstChunkSent=%s audioChunks=%s audioBytes=%s",
                    self._text_chunks,
                    self._first_chunk_sent,
                    self._audio_chunks,
                    self._audio_bytes,
                )
            await asyncio.to_thread(self._synthesizer.async_streaming_complete, self._settings.TTS_TIMEOUT * 1000)
            with logging_context(
                trace_id=self._session.trace_id,
                session_id=self._session.session_id,
                terminal_id=self._session.terminal_id,
            ):
                logger.info("TTS realtime downstream complete requested; waiting callback timeout=%ss", self._settings.TTS_TIMEOUT)
            await asyncio.wait_for(self.complete_event.wait(), timeout=self._settings.TTS_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise RelayError(504, "TTS finish timeout", ErrorCode.TTS_TIMEOUT) from exc
        if self._error is not None:
            raise self._error
        if self._start_time:
            metrics.record("tts_total_ms", (time.perf_counter() - self._start_time) * 1000)
        with logging_context(
            trace_id=self._session.trace_id,
            session_id=self._session.session_id,
            terminal_id=self._session.terminal_id,
        ):
            logger.info(
                "TTS realtime finish completed audioChunks=%s audioBytes=%s",
                self._audio_chunks,
                self._audio_bytes,
            )

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
        with logging_context(
            trace_id=self._session.trace_id,
            session_id=self._session.session_id,
            terminal_id=self._session.terminal_id,
        ):
            logger.info(
                "TTS realtime closing cancelled=%s firstChunkSent=%s audioChunks=%s audioBytes=%s",
                self._cancelled,
                self._first_chunk_sent,
                self._audio_chunks,
                self._audio_bytes,
            )
        if self._synthesizer is not None:
            try:
                await asyncio.to_thread(self._synthesizer.close)
            except Exception:
                logger.debug("ignore TTS close failure", exc_info=True)
            self._synthesizer = None

    async def handle_open(self) -> None:
        with logging_context(
            trace_id=self._session.trace_id,
            session_id=self._session.session_id,
            terminal_id=self._session.terminal_id,
        ):
            logger.info("TTS realtime downstream websocket opened")

    async def handle_complete(self) -> None:
        with logging_context(
            trace_id=self._session.trace_id,
            session_id=self._session.session_id,
            terminal_id=self._session.terminal_id,
        ):
            logger.info(
                "TTS realtime downstream complete callback audioChunks=%s audioBytes=%s",
                self._audio_chunks,
                self._audio_bytes,
            )

    async def handle_close(self) -> None:
        with logging_context(
            trace_id=self._session.trace_id,
            session_id=self._session.session_id,
            terminal_id=self._session.terminal_id,
        ):
            logger.warning(
                "TTS realtime downstream websocket closed firstChunkSent=%s audioChunks=%s audioBytes=%s",
                self._first_chunk_sent,
                self._audio_chunks,
                self._audio_bytes,
            )

    async def handle_event(self, message: str) -> None:
        with logging_context(
            trace_id=self._session.trace_id,
            session_id=self._session.session_id,
            terminal_id=self._session.terminal_id,
        ):
            logger.debug("TTS realtime event: %s", message)

    async def handle_error(self, message: str) -> None:
        with logging_context(
            trace_id=self._session.trace_id,
            session_id=self._session.session_id,
            terminal_id=self._session.terminal_id,
        ):
            logger.error("TTS realtime downstream error: %r", message)
        self._error = RelayError(502, "TTS realtime error", ErrorCode.TTS_FAIL)
        await self._ws.send_text(
            ws_error_msg(
                ErrorCode.TTS_FAIL,
                message if isinstance(message, str) else "TTS realtime error",
                session_id=self._session.session_id,
                trace_id=self._session.trace_id,
            )
        )

    async def _send_tts_start(self) -> None:
        if self._tts_start_sent:
            return
        self._tts_start_sent = True
        codec, sample_rate = _resolve_codec_info(self._settings.TTS_AUDIO_FORMAT)
        msg = {
            "type": "tts_start",
            "sessionId": self._session.session_id,
            "traceId": self._session.trace_id,
            "roundId": self._session.round_id,
            "codec": codec,
            "sampleRate": sample_rate,
            "channels": 1,
        }
        with logging_context(
            trace_id=self._session.trace_id,
            session_id=self._session.session_id,
            terminal_id=self._session.terminal_id,
        ):
            logger.info("TTS realtime upstream tts_start codec=%s sampleRate=%s channels=%s", codec, sample_rate, 1)
        await self._ws.send_text(json.dumps(msg, ensure_ascii=False))

    async def send_tts_end(self, reason: str = "completed") -> None:
        if not self._tts_start_sent:
            return
        msg = {
            "type": "tts_end",
            "sessionId": self._session.session_id,
            "traceId": self._session.trace_id,
            "roundId": self._session.round_id,
            "reason": reason,
        }
        with logging_context(
            trace_id=self._session.trace_id,
            session_id=self._session.session_id,
            terminal_id=self._session.terminal_id,
        ):
            logger.info("TTS realtime upstream tts_end reason=%s audioChunks=%s audioBytes=%s", reason, self._audio_chunks, self._audio_bytes)
        await self._ws.send_text(json.dumps(msg, ensure_ascii=False))

    async def handle_audio_chunk(self, data: bytes) -> None:
        if self._cancelled:
            return
        self._audio_chunks += 1
        self._audio_bytes += len(data)
        if not self._first_chunk_sent and self._start_time:
            metrics.record("tts_first_chunk_ms", (time.perf_counter() - self._start_time) * 1000)
            self._first_chunk_sent = True
            self.first_chunk_event.set()
            with logging_context(
                trace_id=self._session.trace_id,
                session_id=self._session.session_id,
                terminal_id=self._session.terminal_id,
            ):
                logger.info("TTS realtime first audio chunk bytes=%s", len(data))
            await self._send_tts_start()
        elif self._audio_chunks <= 3:
            with logging_context(
                trace_id=self._session.trace_id,
                session_id=self._session.session_id,
                terminal_id=self._session.terminal_id,
            ):
                logger.info("TTS realtime audio chunk index=%s bytes=%s", self._audio_chunks, len(data))
        await self._ws.send_bytes(data)
