import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.errors import RelayError, catch_all, relay_error_handler, timeout_handler
from app.core.http_client import start_client, stop_client
from app.core.logger import RequestLogMiddleware, setup_logging
from app.core.metrics import metrics
from app.core.security import is_ip_allowed
from app.routers import asr, asr_ws, llm, ocr, tts, tts_qwen_ws, tts_ws, wakeup_ws
from app.services.degrade_service import DegradeService
from app.services.session_service import SessionService

logger = logging.getLogger("voice-relay")


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)
    await start_client()
    session_service = SessionService(settings)
    cleanup_task = asyncio.create_task(session_service.cleanup_loop())
    application.state.session_service = session_service
    application.state.degrade_service = DegradeService(settings)
    logger.info("voice-relay started on port %d", settings.PORT)
    yield
    cleanup_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await cleanup_task
    await session_service.shutdown(timeout_s=10)
    await stop_client()
    logger.info("voice-relay stopped")


app = FastAPI(title="Voice Relay", lifespan=lifespan)

app.add_exception_handler(RelayError, relay_error_handler)
app.add_exception_handler(httpx.TimeoutException, timeout_handler)
app.add_exception_handler(Exception, catch_all)
@app.middleware("http")
async def ip_whitelist(request: Request, call_next):
    if request.url.path in ("/health", "/docs", "/openapi.json", "/redoc"):
        return await call_next(request)
    client_ip = request.client.host if request.client else "0.0.0.0"
    if not is_ip_allowed(client_ip):
        logger.warning("blocked ip: %s", client_ip)
        return JSONResponse(status_code=403, content={"error": {"code": 403, "message": "forbidden"}})
    return await call_next(request)


app.add_middleware(RequestLogMiddleware)

app.include_router(asr.router)
app.include_router(asr_ws.router)
app.include_router(llm.router)
app.include_router(tts.router)
app.include_router(tts_ws.router)
app.include_router(tts_qwen_ws.router)
if get_settings().WAKEUP_ENABLED:
    app.include_router(wakeup_ws.router)
if get_settings().OCR_ENABLED:
    app.include_router(ocr.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics_view():
    return metrics.snapshot()
