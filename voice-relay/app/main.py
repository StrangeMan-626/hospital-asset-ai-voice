import ipaddress
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx

from app.core.config import get_settings
from app.core.http_client import start_client, stop_client
from app.core.errors import RelayError, relay_error_handler, timeout_handler, catch_all
from app.core.logger import setup_logging, RequestLogMiddleware
from app.routers import asr, llm, tts

logger = logging.getLogger("voice-relay")


@asynccontextmanager
async def lifespan(application: FastAPI):
    setup_logging(get_settings().LOG_LEVEL)
    await start_client()
    logger.info("voice-relay started on port %d", get_settings().PORT)
    yield
    await stop_client()
    logger.info("voice-relay stopped")


app = FastAPI(title="Voice Relay", lifespan=lifespan)

app.add_exception_handler(RelayError, relay_error_handler)
app.add_exception_handler(httpx.TimeoutException, timeout_handler)
app.add_exception_handler(Exception, catch_all)


def _parse_allowed_networks(raw: str) -> list:
    nets = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            pass
    return nets


_allowed_nets: list | None = None


def _get_allowed_nets():
    global _allowed_nets
    if _allowed_nets is None:
        _allowed_nets = _parse_allowed_networks(get_settings().ALLOWED_IPS)
    return _allowed_nets


@app.middleware("http")
async def ip_whitelist(request: Request, call_next):
    if request.url.path in ("/health", "/docs", "/openapi.json", "/redoc"):
        return await call_next(request)
    client_ip = request.client.host if request.client else "0.0.0.0"
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return JSONResponse(status_code=403, content={"error": {"code": 403, "message": "forbidden"}})
    if not any(addr in net for net in _get_allowed_nets()):
        logger.warning("blocked ip: %s", client_ip)
        return JSONResponse(status_code=403, content={"error": {"code": 403, "message": "forbidden"}})
    return await call_next(request)


app.add_middleware(RequestLogMiddleware)

app.include_router(asr.router)
app.include_router(llm.router)
app.include_router(tts.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
