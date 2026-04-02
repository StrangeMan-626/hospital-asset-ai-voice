import asyncio
import contextlib
import json
import logging
import time

from dashscope.audio.asr import Recognition, RecognitionCallback
from fastapi import WebSocket

from app.core.config import Settings
from app.core.dashscope_config import configure_dashscope
from app.core.errors import ErrorCode, RelayError, ws_error_msg
from app.core.logger import logging_context
from app.core.metrics import metrics
from app.models.session import Session

logger = logging.getLogger("voice-relay")


class _RecognitionBridge(RecognitionCallback):
    def __init__(self, owner: "ASRRealtimeSession"):
        self._owner = owner

    def on_open(self) -> None:
        if self._owner.loop is not None:
            self._owner.loop.call_soon_threadsafe(self._owner.open_event.set)

    def on_complete(self) -> None:
        if self._owner.loop is not None:
            asyncio.run_coroutine_threadsafe(self._owner.handle_complete(), self._owner.loop)

    def on_error(self, result) -> None:
        if self._owner.loop is not None:
            asyncio.run_coroutine_threadsafe(self._owner.handle_error(result), self._owner.loop)

    def on_close(self) -> None:
        return

    def on_event(self, result) -> None:
        sentence = result.get_sentence()
        if sentence is None or self._owner.loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._owner.handle_sentence(sentence), self._owner.loop)


class ASRRealtimeSession:
    def __init__(self, session: Session, ws: WebSocket, settings: Settings):
        self._session = session
        self._ws = ws
        self._settings = settings
        self._recognition: Recognition | None = None
        self._bridge = _RecognitionBridge(self)
        self._start_error: RelayError | None = None
        self._last_partial_text = ""
        self._stop_requested = False
        self._first_partial_sent = False
        self._closed = False
        self._start_time = 0.0
        self._final_wait_task: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.open_event = asyncio.Event()
        self.final_event = asyncio.Event()
        self.complete_event = asyncio.Event()

    async def start(
        self,
        hotwords: list[str] | None = None,
        max_end_silence_ms: int | None = None,
    ) -> None:
        del hotwords
        configure_dashscope(self._settings)
        self.loop = asyncio.get_running_loop()
        self._start_time = time.perf_counter()
        self.open_event.clear()
        self.final_event.clear()
        self.complete_event.clear()
        self._start_error = None
        self._first_partial_sent = False
        self._recognition = Recognition(
            model=self._settings.ASR_REALTIME_MODEL,
            callback=self._bridge,
            format="pcm",
            sample_rate=16000,
        )
        kwargs = {"max_end_silence": max_end_silence_ms or self._settings.ASR_MAX_END_SILENCE_MS}
        try:
            await asyncio.to_thread(self._recognition.start, **kwargs)
        except Exception as exc:
            raise RelayError(502, f"ASR realtime start failed: {exc}", ErrorCode.ASR_CONNECT_FAIL) from exc
        try:
            await asyncio.wait_for(self.open_event.wait(), timeout=self._settings.ASR_TIMEOUT)
        except asyncio.TimeoutError as exc:
            await self.close()
            raise RelayError(504, "ASR realtime start timeout", ErrorCode.ASR_TIMEOUT) from exc
        if self._start_error is not None:
            raise self._start_error

    async def feed_audio(self, pcm_data: bytes) -> None:
        if self._closed or self._recognition is None:
            raise RelayError(400, "ASR session is closed", ErrorCode.ASR_SESSION_CLOSED)
        if self._stop_requested:
            return
        self._session.touch()
        try:
            await asyncio.to_thread(self._recognition.send_audio_frame, pcm_data)
        except Exception as exc:
            raise RelayError(502, f"ASR audio push failed: {exc}", ErrorCode.ASR_CONNECT_FAIL) from exc

    async def stop(self) -> None:
        if self._closed:
            return
        self._stop_requested = True
        self.final_event.clear()
        if self._final_wait_task is not None and not self._final_wait_task.done():
            self._final_wait_task.cancel()
        self._final_wait_task = asyncio.create_task(self._wait_for_final())

    async def speech_resume(self) -> None:
        self._stop_requested = False
        if self._final_wait_task is not None and not self._final_wait_task.done():
            self._final_wait_task.cancel()
        self.final_event.clear()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._final_wait_task is not None and not self._final_wait_task.done():
            self._final_wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._final_wait_task
        if self._recognition is not None:
            try:
                await asyncio.to_thread(self._recognition.stop)
            except Exception:
                logger.debug("ignore ASR recognition stop failure", exc_info=True)
            self._recognition = None

    async def handle_complete(self) -> None:
        self.complete_event.set()

    async def handle_error(self, result) -> None:
        code = getattr(result, "code", "") or ErrorCode.ASR_CONNECT_FAIL
        message = getattr(result, "message", "") or "ASR realtime error"
        self._start_error = RelayError(502, message, code)
        self.open_event.set()
        await self._ws.send_text(
            ws_error_msg(code, message, session_id=self._session.session_id, trace_id=self._session.trace_id)
        )

    async def handle_sentence(self, sentence) -> None:
        text = (sentence or {}).get("text", "")
        if not text:
            return
        self._session.touch()
        elapsed_ms = (time.perf_counter() - self._start_time) * 1000
        is_final = Recognition.is_sentence_end(sentence)
        if is_final:
            metrics.record("asr_final_ms", elapsed_ms)
            self.final_event.set()
            payload = {
                "type": "final",
                "text": text,
                "sessionId": self._session.session_id,
                "traceId": self._session.trace_id,
                "fallbackFromPartial": False,
            }
            self._last_partial_text = ""
        else:
            if not self._first_partial_sent:
                metrics.record("asr_first_partial_ms", elapsed_ms)
                self._first_partial_sent = True
            self._last_partial_text = text
            payload = {
                "type": "partial",
                "text": text,
                "sessionId": self._session.session_id,
                "traceId": self._session.trace_id,
            }
        with logging_context(
            trace_id=self._session.trace_id,
            session_id=self._session.session_id,
            terminal_id=self._session.terminal_id,
        ):
            logger.info("ASR realtime %s", payload["type"])
        await self._ws.send_text(json.dumps(payload, ensure_ascii=False))

    async def _wait_for_final(self) -> None:
        try:
            await asyncio.wait_for(self.final_event.wait(), timeout=self._settings.ASR_FINAL_WAIT_MS / 1000)
        except asyncio.TimeoutError:
            if self._last_partial_text:
                payload = {
                    "type": "final",
                    "text": self._last_partial_text,
                    "sessionId": self._session.session_id,
                    "traceId": self._session.trace_id,
                    "fallbackFromPartial": True,
                }
                await self._ws.send_text(json.dumps(payload, ensure_ascii=False))
            else:
                await self._ws.send_text(
                    ws_error_msg(
                        ErrorCode.ASR_TIMEOUT,
                        "ASR final wait timeout",
                        session_id=self._session.session_id,
                        trace_id=self._session.trace_id,
                    )
                )
        finally:
            self._stop_requested = False
            self._final_wait_task = None
