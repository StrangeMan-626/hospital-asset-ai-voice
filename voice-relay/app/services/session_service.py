import asyncio
import logging
import threading
import time

from fastapi import WebSocket

from app.core.errors import ErrorCode, ws_error_msg
from app.core.metrics import metrics
from app.models.session import Session, SessionState

logger = logging.getLogger("voice-relay")


class SessionService:
    def __init__(self, settings):
        self._sessions: dict[str, Session] = {}
        self._connections: dict[str, WebSocket] = {}
        self._asr_semaphore = asyncio.BoundedSemaphore(settings.SESSION_MAX_CONCURRENT)
        self._tts_semaphore = asyncio.BoundedSemaphore(settings.SESSION_MAX_CONCURRENT)
        self._suspend_ttl_ms = settings.SESSION_SUSPEND_TTL_MS
        self._heartbeat_timeout_s = settings.SESSION_HEARTBEAT_TIMEOUT_MS / 1000
        self._asr_active_count = 0
        self._tts_active_count = 0
        self._count_lock = threading.Lock()

    async def acquire_asr(self) -> bool:
        try:
            await asyncio.wait_for(self._asr_semaphore.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            return False
        with self._count_lock:
            self._asr_active_count += 1
            metrics.set_active_asr_sessions(self._asr_active_count)
        return True

    def release_asr(self) -> None:
        with self._count_lock:
            if self._asr_active_count <= 0:
                return
            self._asr_active_count -= 1
            metrics.set_active_asr_sessions(self._asr_active_count)
        try:
            self._asr_semaphore.release()
        except ValueError:
            logger.warning("ASR semaphore over-release detected")

    async def acquire_tts(self) -> bool:
        try:
            await asyncio.wait_for(self._tts_semaphore.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            return False
        with self._count_lock:
            self._tts_active_count += 1
            metrics.set_active_tts_sessions(self._tts_active_count)
        return True

    def release_tts(self) -> None:
        with self._count_lock:
            if self._tts_active_count <= 0:
                return
            self._tts_active_count -= 1
            metrics.set_active_tts_sessions(self._tts_active_count)
        try:
            self._tts_semaphore.release()
        except ValueError:
            logger.warning("TTS semaphore over-release detected")

    def register(self, session: Session) -> None:
        self._sessions[session.session_id] = session

    def attach_connection(self, session_id: str, ws: WebSocket) -> None:
        self._connections[session_id] = ws

    def detach_connection(self, session_id: str) -> None:
        self._connections.pop(session_id, None)

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._connections.pop(session_id, None)

    async def cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            stale_ids: list[str] = []
            for session_id, session in list(self._sessions.items()):
                expired = session.is_expired(self._suspend_ttl_ms)
                zombie = now - session.last_active_at > self._heartbeat_timeout_s
                if not expired and not zombie and session.state != SessionState.CLOSED:
                    continue
                if session.state != SessionState.CLOSED:
                    session.close()
                ws = self._connections.get(session_id)
                if ws is not None:
                    try:
                        await ws.send_text(
                            ws_error_msg(
                                ErrorCode.SESSION_EXPIRED,
                                "session expired",
                                session_id=session.session_id,
                                trace_id=session.trace_id,
                            )
                        )
                    except Exception:
                        logger.debug("skip sending session expired message", exc_info=True)
                    try:
                        await ws.close(code=1001, reason="session expired")
                    except Exception:
                        logger.debug("skip closing expired websocket", exc_info=True)
                stale_ids.append(session_id)
            for session_id in stale_ids:
                self.remove(session_id)

    async def shutdown(self, timeout_s: int = 10) -> None:
        deadline = time.monotonic() + timeout_s
        for session_id, session in list(self._sessions.items()):
            ws = self._connections.get(session_id)
            if ws is not None:
                try:
                    await ws.send_text(
                        ws_error_msg(
                            ErrorCode.INTERNAL_ERROR,
                            "service shutting down",
                            session_id=session.session_id,
                            trace_id=session.trace_id,
                        )
                    )
                except Exception:
                    logger.debug("skip sending shutdown message", exc_info=True)
                try:
                    await ws.close(code=1012, reason="service shutting down")
                except Exception:
                    logger.debug("skip closing websocket on shutdown", exc_info=True)
            session.close()
            self.remove(session_id)
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0)

    @property
    def active_asr_count(self) -> int:
        return self._asr_active_count

    @property
    def active_tts_count(self) -> int:
        return self._tts_active_count
