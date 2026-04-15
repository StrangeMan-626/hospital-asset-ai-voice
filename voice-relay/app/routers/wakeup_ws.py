import asyncio
import json
import time
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.services.wakeup_service import get_kws, decode_pcm
from app.core.config import get_settings

router = APIRouter()
logger = logging.getLogger("wakeup")

_active_connections = 0
_lock = asyncio.Lock()


@router.websocket("/wakeup")
async def wakeup_ws(ws: WebSocket):
    global _active_connections
    settings = get_settings()

    async with _lock:
        if _active_connections >= settings.WAKEUP_MAX_CONNECTIONS:
            await ws.close(code=1008, reason="SESSION_LIMIT")
            return
        _active_connections += 1

    terminal_id = "unknown"
    try:
        kws = get_kws()
    except Exception:
        async with _lock:
            _active_connections -= 1
        await ws.close(code=1011, reason="MODEL_UNAVAILABLE")
        return
    stream = kws.create_stream()
    paused = False

    try:
        await ws.accept()
        logger.info("wakeup connection opened")
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if "text" in msg:
                data = json.loads(msg["text"])
                msg_type = data.get("type")
                if msg_type == "start":
                    terminal_id = data.get("terminalId", "unknown")
                    stream = kws.create_stream()
                    paused = False
                    logger.info(f"[{terminal_id}] wakeup started, wakeWord={data.get('wakeWord')}")
                elif msg_type == "pause":
                    paused = True
                elif msg_type == "resume":
                    paused = False
                    stream = kws.create_stream()
                elif msg_type == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
            elif "bytes" in msg:
                if paused:
                    continue
                samples = decode_pcm(msg["bytes"])
                stream.accept_waveform(16000, samples)
                t0 = time.monotonic()
                while kws.is_ready(stream):
                    kws.decode_stream(stream)
                result = kws.get_result(stream)
                if result:
                    cost_ms = int((time.monotonic() - t0) * 1000)
                    payload = {
                        "type": "wake_detected",
                        "wakeWord": result.strip(),
                        "confidence": 0.9,
                        "costMs": cost_ms,
                    }
                    await ws.send_text(json.dumps(payload))
                    logger.info(f"[{terminal_id}] wake_detected: {result.strip()}, costMs={cost_ms}")
                    stream = kws.create_stream()
    except WebSocketDisconnect:
        pass
    except RuntimeError as exc:
        if 'Cannot call "receive" once a disconnect message has been received' not in str(exc):
            raise
        pass
    finally:
        async with _lock:
            _active_connections -= 1
        logger.info(f"[{terminal_id}] wakeup connection closed")
