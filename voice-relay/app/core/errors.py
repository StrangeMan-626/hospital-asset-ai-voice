import json
import logging

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("voice-relay")


class ErrorCode:
    ASR_CONNECT_FAIL = "ASR_CONNECT_FAIL"
    ASR_TIMEOUT = "ASR_TIMEOUT"
    ASR_BAD_AUDIO = "ASR_BAD_AUDIO"
    ASR_SESSION_CLOSED = "ASR_SESSION_CLOSED"
    TTS_CONNECT_FAIL = "TTS_CONNECT_FAIL"
    TTS_TIMEOUT = "TTS_TIMEOUT"
    TTS_FAIL = "TTS_FAIL"
    TTS_CANCELLED = "TTS_CANCELLED"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_BAD_RESPONSE = "LLM_BAD_RESPONSE"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_LIMIT = "SESSION_LIMIT"
    CONNECT_POOL_EXHAUSTED = "CONNECT_POOL_EXHAUSTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    OCR_BAD_IMAGE = "OCR_BAD_IMAGE"
    OCR_RECOGNIZE_FAIL = "OCR_RECOGNIZE_FAIL"


class RelayError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        code: str = ErrorCode.INTERNAL_ERROR,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


def ws_error_payload(
    code: str,
    message: str,
    session_id: str = "",
    trace_id: str = "",
) -> dict[str, str]:
    return {
        "type": "error",
        "code": code,
        "message": message,
        "sessionId": session_id,
        "traceId": trace_id,
    }


def ws_error_msg(
    code: str,
    message: str,
    session_id: str = "",
    trace_id: str = "",
) -> str:
    return json.dumps(
        ws_error_payload(code, message, session_id=session_id, trace_id=trace_id),
        ensure_ascii=False,
    )


def _err(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


async def relay_error_handler(_: Request, exc: RelayError) -> JSONResponse:
    logger.warning("RelayError %s: %s", exc.code, exc.message)
    return _err(exc.status_code, exc.code, exc.message)


async def timeout_handler(_: Request, exc: httpx.TimeoutException) -> JSONResponse:
    logger.error("Upstream timeout: %s", exc)
    return _err(504, ErrorCode.LLM_TIMEOUT, "upstream timeout")


async def catch_all(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception")
    return _err(500, ErrorCode.INTERNAL_ERROR, "internal error")
