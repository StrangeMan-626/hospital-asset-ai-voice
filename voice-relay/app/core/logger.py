import contextlib
import contextvars
import logging
import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")
_session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("session_id", default="-")
_terminal_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("terminal_id", default="-")
_record_factory_installed = False


def generate_trace_id() -> str:
    return f"relay-{uuid.uuid4().hex[:12]}"


def set_log_context(
    trace_id: str = "",
    session_id: str = "",
    terminal_id: str = "",
) -> tuple[contextvars.Token, contextvars.Token, contextvars.Token]:
    return (
        _trace_id_var.set(trace_id or "-"),
        _session_id_var.set(session_id or "-"),
        _terminal_id_var.set(terminal_id or "-"),
    )


def reset_log_context(tokens: tuple[contextvars.Token, contextvars.Token, contextvars.Token]) -> None:
    _trace_id_var.reset(tokens[0])
    _session_id_var.reset(tokens[1])
    _terminal_id_var.reset(tokens[2])


@contextlib.contextmanager
def logging_context(trace_id: str = "", session_id: str = "", terminal_id: str = ""):
    tokens = set_log_context(trace_id=trace_id, session_id=session_id, terminal_id=terminal_id)
    try:
        yield
    finally:
        reset_log_context(tokens)


def setup_logging(level: str = "INFO") -> None:
    global _record_factory_installed
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format=(
                "%(asctime)s | %(levelname)s | traceId=%(traceId)s | "
                "sessionId=%(sessionId)s | terminalId=%(terminalId)s | %(message)s"
            ),
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    else:
        root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        for handler in root_logger.handlers:
            handler.setFormatter(
                logging.Formatter(
                    fmt=(
                        "%(asctime)s | %(levelname)s | traceId=%(traceId)s | "
                        "sessionId=%(sessionId)s | terminalId=%(terminalId)s | %(message)s"
                    ),
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
    if not _record_factory_installed:
        previous_factory = logging.getLogRecordFactory()

        def record_factory(*args, **kwargs):
            record = previous_factory(*args, **kwargs)
            record.traceId = _trace_id_var.get()
            record.sessionId = _session_id_var.get()
            record.terminalId = _terminal_id_var.get()
            return record

        logging.setLogRecordFactory(record_factory)
        _record_factory_installed = True


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.perf_counter()
        trace_id = request.headers.get("x-trace-id") or generate_trace_id()
        session_id = request.headers.get("x-session-id", "")
        terminal_id = request.headers.get("x-terminal-id", "")
        request.state.trace_id = trace_id

        with logging_context(trace_id=trace_id, session_id=session_id, terminal_id=terminal_id):
            response = await call_next(request)
            response.headers["X-Trace-Id"] = trace_id
            ms = (time.perf_counter() - start) * 1000
            ip = request.client.host if request.client else "-"
            logging.getLogger("voice-relay").info(
                "%s | %s %s | %d | %.0fms",
                ip,
                request.method,
                request.url.path,
                response.status_code,
                ms,
            )

        return response
