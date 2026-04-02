import enum
import logging
import time
import uuid

from app.core.logger import generate_trace_id, logging_context

logger = logging.getLogger("voice-relay")


class SessionState(str, enum.Enum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"


class Session:
    def __init__(self, session_id: str | None = None, terminal_id: str = ""):
        self.session_id = session_id or str(uuid.uuid4())
        self.terminal_id = terminal_id
        self.trace_id = generate_trace_id()
        self.state = SessionState.CREATED
        self.created_at = time.monotonic()
        self.last_active_at = self.created_at
        self.suspended_at: float | None = None
        self.reconnect_count = 0

    def _transition(self, state: SessionState) -> None:
        previous = self.state
        self.state = state
        with logging_context(
            trace_id=self.trace_id,
            session_id=self.session_id,
            terminal_id=self.terminal_id,
        ):
            logger.info("session state: %s -> %s", previous, state)

    def activate(self) -> None:
        if self.state == SessionState.CLOSED:
            raise RuntimeError("closed session cannot be activated")
        if self.state == SessionState.SUSPENDED:
            self.reconnect_count += 1
        self.suspended_at = None
        self.touch()
        self._transition(SessionState.ACTIVE)

    def suspend(self) -> None:
        if self.state == SessionState.CLOSED:
            return
        self.touch()
        self.suspended_at = time.monotonic()
        self._transition(SessionState.SUSPENDED)

    def resume(self) -> None:
        if self.state == SessionState.CLOSED:
            raise RuntimeError("closed session cannot be resumed")
        self.reconnect_count += 1
        self.suspended_at = None
        self.touch()
        self._transition(SessionState.ACTIVE)

    def close(self) -> None:
        if self.state == SessionState.CLOSED:
            return
        self.suspended_at = None
        self.touch()
        self._transition(SessionState.CLOSED)

    def touch(self) -> None:
        self.last_active_at = time.monotonic()

    def is_expired(self, ttl_ms: int) -> bool:
        if self.state != SessionState.SUSPENDED or self.suspended_at is None:
            return False
        return (time.monotonic() - self.suspended_at) * 1000 > ttl_ms
