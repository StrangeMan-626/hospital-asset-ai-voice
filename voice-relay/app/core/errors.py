import logging
from fastapi import Request
from fastapi.responses import JSONResponse
import httpx

logger = logging.getLogger("voice-relay")


class RelayError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message


def _err(code: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"error": {"code": code, "message": msg}})


async def relay_error_handler(_: Request, exc: RelayError) -> JSONResponse:
    logger.warning("RelayError %d: %s", exc.status_code, exc.message)
    return _err(exc.status_code, exc.message)


async def timeout_handler(_: Request, exc: httpx.TimeoutException) -> JSONResponse:
    logger.error("Upstream timeout: %s", exc)
    return _err(504, "upstream timeout")


async def catch_all(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception")
    return _err(500, str(exc))
