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
    disconnected = False
    audio_chunks = 0
    audio_bytes = 0
    dropped_paused_chunks = 0
    last_audio_log_at = 0.0

    try:
        await ws.accept()
        logger.info("wakeup connection opened")
        while True:
            try:
                msg = await ws.receive()
            except RuntimeError as exc:
                if 'disconnect message has been received' in str(exc):
                    disconnected = True
                    break
                raise
            if msg["type"] == "websocket.disconnect":
                disconnected = True
                break
            if msg["type"] != "websocket.receive":
                continue

            if msg.get("text") is not None:
                data = json.loads(msg["text"])
                msg_type = data.get("type")
                if msg_type == "start":
                    terminal_id = data.get("terminalId", "unknown")
                    sample_rate = data.get("sampleRate")
                    channels = data.get("channels")
                    stream = kws.create_stream()
                    paused = False
                    audio_chunks = 0
                    audio_bytes = 0
                    dropped_paused_chunks = 0
                    last_audio_log_at = 0.0
                    logger.info(
                        "[%s] wakeup started, wakeWord=%s sampleRate=%s channels=%s",
                        terminal_id,
                        data.get("wakeWord"),
                        sample_rate,
                        channels,
                    )
                    if sample_rate not in (None, 16000) or channels not in (None, 1):
                        logger.warning(
                            "[%s] wakeup received unsupported audio format sampleRate=%s channels=%s; expected pcm16 16k mono",
                            terminal_id,
                            sample_rate,
                            channels,
                        )
                elif msg_type == "pause":
                    paused = True
                    logger.info(
                        "[%s] wakeup paused audioChunks=%s audioBytes=%s",
                        terminal_id,
                        audio_chunks,
                        audio_bytes,
                    )
                elif msg_type == "resume":
                    paused = False
                    stream = kws.create_stream()
                    logger.info(
                        "[%s] wakeup resumed audioChunks=%s audioBytes=%s",
                        terminal_id,
                        audio_chunks,
                        audio_bytes,
                    )
                elif msg_type == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
            elif msg.get("bytes") is not None:
                audio_chunks += 1
                audio_bytes += len(msg["bytes"])
                if paused:
                    dropped_paused_chunks += 1
                    if dropped_paused_chunks == 1 or dropped_paused_chunks % 50 == 0:
                        logger.warning(
                            "[%s] wakeup dropping audio while paused droppedChunks=%s totalChunks=%s",
                            terminal_id,
                            dropped_paused_chunks,
                            audio_chunks,
                        )
                    continue
                if len(msg["bytes"]) % 2 != 0:
                    logger.warning(
                        "[%s] wakeup received odd-length pcm bytes=%s",
                        terminal_id,
                        len(msg["bytes"]),
                    )
                now = time.monotonic()
                if audio_chunks == 1 or now - last_audio_log_at >= 5:
                    last_audio_log_at = now
                    logger.info(
                        "[%s] wakeup audio flowing chunks=%s bytes=%s",
                        terminal_id,
                        audio_chunks,
                        audio_bytes,
                    )
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
        disconnected = True
    finally:
        async with _lock:
            _active_connections -= 1
        logger.info(
            "[%s] wakeup connection closed disconnected=%s audioChunks=%s audioBytes=%s droppedPausedChunks=%s",
            terminal_id,
            disconnected,
            audio_chunks,
            audio_bytes,
            dropped_paused_chunks,
        )
