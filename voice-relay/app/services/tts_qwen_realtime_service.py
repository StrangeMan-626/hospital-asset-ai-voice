import asyncio
import base64
import json
import logging
import time

from dashscope.audio.qwen_tts_realtime import AudioFormat, QwenTtsRealtime, QwenTtsRealtimeCallback
from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.core.config import Settings
from app.core.dashscope_config import configure_dashscope
from app.core.errors import ErrorCode, RelayError, ws_error_msg
from app.core.logger import logging_context
from app.core.metrics import metrics
from app.models.session import Session

logger = logging.getLogger("voice-relay")


def _resolve_qwen_codec_info(fmt: str, sample_rate: int) -> tuple[str, int]:
    normalized = (fmt or "pcm").lower()
    if normalized == "pcm":
        return "pcm_s16le", sample_rate
    if normalized == "wav":
        return "wav", sample_rate
    if normalized == "opus":
        return "audio/ogg", sample_rate
    return "audio/mpeg", sample_rate


class _QwenTTSBridge(QwenTtsRealtimeCallback):
    def __init__(self, owner: "QwenTTSRealtimeSession"):
        self._owner = owner

    def on_open(self) -> None:
        if self._owner.loop is not None:
            asyncio.run_coroutine_threadsafe(self._owner.handle_open(), self._owner.loop)

    def on_close(self, close_status_code, close_msg) -> None:
        if self._owner.loop is not None:
            asyncio.run_coroutine_threadsafe(
                self._owner.handle_close(close_status_code, close_msg),
                self._owner.loop,
            )

    def on_event(self, message) -> None:
        if self._owner.loop is not None:
            asyncio.run_coroutine_threadsafe(self._owner.handle_event(message), self._owner.loop)


class QwenTTSRealtimeSession:
    def __init__(self, session: Session, ws: WebSocket, settings: Settings):
        self._session = session
        self._ws = ws
        self._settings = settings
        self._client: QwenTtsRealtime | None = None
        self._bridge = _QwenTTSBridge(self)
        self._first_chunk_sent = False
        self._tts_start_sent = False
        self._cancelled = False
        self._closed = False
        self._session_finished = False
        self._error: RelayError | None = None
        self._start_time = 0.0
        self._text_chunks = 0
        self._audio_chunks = 0
        self._audio_bytes = 0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.complete_event = asyncio.Event()
        self.first_chunk_event = asyncio.Event()
        self.session_ready_event = asyncio.Event()

    async def start(self) -> None:
        self._validate_settings()
        configure_dashscope(self._settings)
        self.loop = asyncio.get_running_loop()
        self.complete_event.clear()
        self.first_chunk_event.clear()
        self.session_ready_event.clear()
        self._reset_state()
        self._client = QwenTtsRealtime(
            model=self._settings.QWEN_TTS_REALTIME_MODEL,
            callback=self._bridge,
            url=self._settings.qwen_tts_realtime_ws_url,
        )
        with self._log_context():
            logger.info(
                "Qwen TTS realtime init model=%s voice=%s format=%s sampleRate=%s mode=%s url=%s timeout=%ss firstChunkTimeout=%ss",
                self._settings.QWEN_TTS_REALTIME_MODEL,
                self._settings.QWEN_TTS_REALTIME_VOICE,
                self._settings.QWEN_TTS_REALTIME_AUDIO_FORMAT,
                self._settings.QWEN_TTS_REALTIME_SAMPLE_RATE,
                self._settings.QWEN_TTS_REALTIME_MODE,
                self._settings.qwen_tts_realtime_ws_url,
                self._settings.QWEN_TTS_REALTIME_TIMEOUT,
                self._settings.QWEN_TTS_REALTIME_FIRST_CHUNK_TIMEOUT,
            )
        try:
            await asyncio.to_thread(self._client.connect)
            await asyncio.to_thread(
                self._client.update_session,
                self._settings.QWEN_TTS_REALTIME_VOICE,
                AudioFormat.PCM_24000HZ_MONO_16BIT,
                self._settings.QWEN_TTS_REALTIME_MODE,
                self._settings.QWEN_TTS_REALTIME_SAMPLE_RATE,
                None,
                None,
                self._settings.QWEN_TTS_REALTIME_AUDIO_FORMAT,
            )
            await asyncio.wait_for(
                self.session_ready_event.wait(),
                timeout=self._settings.QWEN_TTS_REALTIME_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            raise RelayError(504, "Qwen realtime session init timeout", ErrorCode.TTS_TIMEOUT) from exc
        except TimeoutError as exc:
            raise RelayError(504, "Qwen realtime connect timeout", ErrorCode.TTS_TIMEOUT) from exc
        except RelayError:
            raise
        except Exception as exc:
            raise RelayError(502, f"Qwen realtime start failed: {exc}", ErrorCode.TTS_CONNECT_FAIL) from exc
        if self._error is not None:
            raise self._error

    async def send_text(self, text: str) -> None:
        if self._closed or self._client is None:
            raise RelayError(400, "Qwen TTS session is closed", ErrorCode.TTS_FAIL)
        if not text.strip():
            raise RelayError(400, "Qwen TTS text content is empty", ErrorCode.TTS_FAIL)
        self._session.touch()
        if self._start_time == 0.0:
            self._start_time = time.perf_counter()
        self._text_chunks += 1
        with self._log_context():
            logger.info(
                "Qwen TTS realtime send_text chunk=%s chars=%s preview=%r",
                self._text_chunks,
                len(text),
                text[:40],
            )
        try:
            await asyncio.to_thread(self._client.append_text, text)
        except Exception as exc:
            raise RelayError(502, f"Qwen realtime send failed: {exc}", ErrorCode.TTS_CONNECT_FAIL) from exc
        if self._error is not None:
            raise self._error

    async def finish(self) -> None:
        if self._closed or self._client is None:
            return
        if self._text_chunks == 0:
            raise RelayError(400, "Qwen TTS finish called before any valid text", ErrorCode.TTS_FAIL)
        try:
            with self._log_context():
                logger.info(
                    "Qwen TTS realtime finish requested textChunks=%s firstChunkSent=%s audioChunks=%s audioBytes=%s",
                    self._text_chunks,
                    self._first_chunk_sent,
                    self._audio_chunks,
                    self._audio_bytes,
                )
            if self._settings.QWEN_TTS_REALTIME_MODE == "commit":
                await asyncio.to_thread(self._client.commit)
            await asyncio.to_thread(self._client.finish)
            if not self._first_chunk_sent:
                await asyncio.wait_for(
                    self.first_chunk_event.wait(),
                    timeout=self._settings.QWEN_TTS_REALTIME_FIRST_CHUNK_TIMEOUT,
                )
            await asyncio.wait_for(
                self.complete_event.wait(),
                timeout=self._settings.QWEN_TTS_REALTIME_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            if not self._first_chunk_sent:
                raise RelayError(504, "Qwen realtime first chunk timeout", ErrorCode.TTS_TIMEOUT) from exc
            raise RelayError(504, "Qwen realtime finish timeout", ErrorCode.TTS_TIMEOUT) from exc
        except Exception as exc:
            raise RelayError(502, f"Qwen realtime finish failed: {exc}", ErrorCode.TTS_FAIL) from exc
        if self._error is not None:
            raise self._error
        if self._start_time:
            metrics.record("tts_total_ms", (time.perf_counter() - self._start_time) * 1000)

    async def cancel(self) -> None:
        self._cancelled = True
        if self._closed or self._client is None:
            return
        try:
            cancel_response = getattr(self._client, "cancel_response", None)
            if cancel_response is not None:
                await asyncio.to_thread(cancel_response)
        except Exception:
            logger.debug("ignore Qwen TTS cancel failure", exc_info=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._log_context():
            logger.info(
                "Qwen TTS realtime closing cancelled=%s sessionFinished=%s firstChunkSent=%s audioChunks=%s audioBytes=%s",
                self._cancelled,
                self._session_finished,
                self._first_chunk_sent,
                self._audio_chunks,
                self._audio_bytes,
            )
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception:
                logger.debug("ignore Qwen TTS close failure", exc_info=True)
            self._client = None

    async def handle_open(self) -> None:
        with self._log_context():
            logger.info("Qwen TTS realtime downstream websocket opened")

    async def handle_close(self, close_status_code, close_msg) -> None:
        expected_close = self._closed or self._cancelled or self._session_finished
        with self._log_context():
            log_fn = logger.info if expected_close else logger.warning
            log_fn(
                "Qwen TTS realtime downstream websocket closed code=%s msg=%s firstChunkSent=%s audioChunks=%s audioBytes=%s sessionFinished=%s cancelled=%s closed=%s",
                close_status_code,
                close_msg,
                self._first_chunk_sent,
                self._audio_chunks,
                self._audio_bytes,
                self._session_finished,
                self._cancelled,
                self._closed,
            )
        if not expected_close:
            await self._set_error(
                RelayError(502, "Qwen realtime connection closed unexpectedly", ErrorCode.TTS_CONNECT_FAIL),
                "Qwen realtime connection closed unexpectedly",
            )

    async def handle_event(self, message) -> None:
        data = message
        if isinstance(message, str):
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                await self._set_error(
                    RelayError(502, "Qwen realtime invalid event payload", ErrorCode.TTS_FAIL),
                    "Qwen realtime invalid event payload",
                )
                return
        if not isinstance(data, dict):
            await self._set_error(
                RelayError(502, "Qwen realtime unexpected event payload", ErrorCode.TTS_FAIL),
                "Qwen realtime unexpected event payload",
            )
            return

        event_type = data.get("type", "")
        with self._log_context():
            if event_type == "response.audio.delta":
                logger.debug("Qwen TTS realtime event=%s deltaChars=%s", event_type, len(data.get("delta", "")))
            else:
                logger.info("Qwen TTS realtime event=%s", event_type)

        if event_type in {"session.created", "session.updated"}:
            self.session_ready_event.set()
            return
        if event_type == "session.finished":
            self._session_finished = True
            self.complete_event.set()
            return
        if event_type == "response.audio.delta":
            delta = data.get("delta")
            if not isinstance(delta, str) or not delta:
                await self._set_error(
                    RelayError(502, "Qwen realtime audio payload is empty", ErrorCode.TTS_FAIL),
                    "Qwen realtime audio payload is empty",
                )
                return
            try:
                audio_bytes = base64.b64decode(delta)
            except Exception as exc:
                await self._set_error(
                    RelayError(502, f"Qwen realtime audio decode failed: {exc}", ErrorCode.TTS_FAIL),
                    "Qwen realtime audio decode failed",
                )
                return
            await self.handle_audio_chunk(audio_bytes)
            return
        if event_type == "error":
            error_payload = data.get("error") if isinstance(data.get("error"), dict) else data
            error_message = str(error_payload.get("message") or error_payload.get("msg") or "Qwen realtime error")
            await self._set_error(RelayError(502, error_message, ErrorCode.TTS_FAIL), error_message)

    async def send_tts_end(self, reason: str = "completed") -> None:
        if not self._tts_start_sent:
            return
        await self._safe_send_text(
            {
                "type": "tts_end",
                "sessionId": self._session.session_id,
                "traceId": self._session.trace_id,
                "roundId": self._session.round_id,
                "reason": reason,
            }
        )

    async def handle_audio_chunk(self, data: bytes) -> None:
        if self._cancelled:
            return
        self._audio_chunks += 1
        self._audio_bytes += len(data)
        if not self._first_chunk_sent and self._start_time:
            metrics.record("tts_first_chunk_ms", (time.perf_counter() - self._start_time) * 1000)
            self._first_chunk_sent = True
            self.first_chunk_event.set()
            with self._log_context():
                logger.info("Qwen TTS realtime first audio chunk bytes=%s", len(data))
            await self._send_tts_start()
        await self._safe_send_bytes(data)

    async def _send_tts_start(self) -> None:
        if self._tts_start_sent:
            return
        self._tts_start_sent = True
        codec, sample_rate = _resolve_qwen_codec_info(
            self._settings.QWEN_TTS_REALTIME_AUDIO_FORMAT,
            self._settings.QWEN_TTS_REALTIME_SAMPLE_RATE,
        )
        await self._safe_send_text(
            {
                "type": "tts_start",
                "sessionId": self._session.session_id,
                "traceId": self._session.trace_id,
                "roundId": self._session.round_id,
                "codec": codec,
                "sampleRate": sample_rate,
                "channels": 1,
                "provider": "qwen_realtime",
            }
        )

    def _validate_settings(self) -> None:
        if not self._settings.QWEN_TTS_REALTIME_MODEL:
            raise RelayError(500, "Qwen realtime model is not configured", ErrorCode.TTS_FAIL)
        if not self._settings.QWEN_TTS_REALTIME_VOICE:
            raise RelayError(500, "Qwen realtime voice is not configured", ErrorCode.TTS_FAIL)
        if self._settings.QWEN_TTS_REALTIME_AUDIO_FORMAT not in ("pcm", "wav", "mp3", "opus"):
            raise RelayError(500, "Qwen realtime audio format is invalid", ErrorCode.TTS_FAIL)
        if self._settings.QWEN_TTS_REALTIME_MODE not in ("server_commit", "commit"):
            raise RelayError(500, "Qwen realtime mode is invalid", ErrorCode.TTS_FAIL)

    def _reset_state(self) -> None:
        self._error = None
        self._first_chunk_sent = False
        self._tts_start_sent = False
        self._cancelled = False
        self._closed = False
        self._session_finished = False
        self._start_time = 0.0
        self._text_chunks = 0
        self._audio_chunks = 0
        self._audio_bytes = 0

    def _log_context(self):
        return logging_context(
            trace_id=self._session.trace_id,
            session_id=self._session.session_id,
            terminal_id=self._session.terminal_id,
        )

    async def _set_error(self, error: RelayError, message: str) -> None:
        if self._error is None:
            self._error = error
        self.first_chunk_event.set()
        self.complete_event.set()
        self.session_ready_event.set()
        with self._log_context():
            logger.error("Qwen TTS realtime downstream error: %s", message)
        await self._safe_send_raw_text(
            ws_error_msg(
                self._error.code,
                message,
                session_id=self._session.session_id,
                trace_id=self._session.trace_id,
            )
        )

    def _upstream_ws_open(self) -> bool:
        return (
            self._ws.client_state == WebSocketState.CONNECTED
            and self._ws.application_state == WebSocketState.CONNECTED
        )

    async def _safe_send_text(self, payload: dict) -> None:
        await self._safe_send_raw_text(json.dumps(payload, ensure_ascii=False))

    async def _safe_send_raw_text(self, payload: str) -> None:
        if not self._upstream_ws_open():
            with self._log_context():
                logger.info("Qwen TTS realtime skip upstream text send because websocket is closed")
            return
        try:
            await self._ws.send_text(payload)
        except RuntimeError:
            with self._log_context():
                logger.info("Qwen TTS realtime ignore upstream text send after websocket close")

    async def _safe_send_bytes(self, data: bytes) -> None:
        if not self._upstream_ws_open():
            with self._log_context():
                logger.info("Qwen TTS realtime skip upstream audio send because websocket is closed")
            return
        try:
            await self._ws.send_bytes(data)
        except RuntimeError:
            with self._log_context():
                logger.info("Qwen TTS realtime ignore upstream audio send after websocket close")
