import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.core.config import get_settings
from app.core.errors import ErrorCode, RelayError, ws_error_msg
from app.core.security import is_ip_allowed
from app.models.session import Session
from app.services.tts_qwen_realtime_service import QwenTTSRealtimeSession

logger = logging.getLogger("voice-relay")
router = APIRouter(tags=["TTS"])
PONG = json.dumps({"type": "pong"})


async def _safe_send_text(ws: WebSocket, payload: str) -> bool:
    if ws.client_state != WebSocketState.CONNECTED or ws.application_state != WebSocketState.CONNECTED:
        return False
    try:
        await ws.send_text(payload)
        return True
    except RuntimeError:
        return False


@router.websocket("/tts/realtime/qwen")
async def tts_realtime_qwen(ws: WebSocket):
    client_ip = ws.client.host if ws.client else "0.0.0.0"
    if not is_ip_allowed(client_ip):
        await ws.close(code=1008, reason="forbidden")
        return

    await ws.accept()
    logger.info("Qwen TTS realtime ws connected ip=%s", client_ip)
    settings = get_settings()
    session_service = ws.app.state.session_service

    session: Session | None = None
    tts_rt: QwenTTSRealtimeSession | None = None
    acquired = False

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg["type"] != "websocket.receive":
                continue
            text = msg.get("text")
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                await _safe_send_text(ws, ws_error_msg(ErrorCode.TTS_FAIL, "invalid json payload"))
                continue

            msg_type = data.get("type")
            if msg_type == "ping":
                await _safe_send_text(ws, PONG)
                continue

            if msg_type == "start":
                if session is not None:
                    await _safe_send_text(
                        ws,
                        ws_error_msg(
                            ErrorCode.TTS_FAIL,
                            "session already started",
                            session_id=session.session_id,
                            trace_id=session.trace_id,
                        )
                    )
                    continue
                if not acquired:
                    acquired = await session_service.acquire_tts()
                if not acquired:
                    await _safe_send_text(ws, ws_error_msg(ErrorCode.SESSION_LIMIT, "TTS session limit reached"))
                    continue
                session = Session(data.get("sessionId"), data.get("terminalId", ""))
                session.round_id = data.get("roundId", "")
                logger.info(
                    "Qwen TTS realtime start received sessionId=%s terminalId=%s roundId=%s",
                    session.session_id,
                    session.terminal_id,
                    session.round_id,
                )
                session_service.register(session)
                session_service.attach_connection(session.session_id, ws)
                session.activate()
                tts_rt = QwenTTSRealtimeSession(session, ws, settings)
                await tts_rt.start()
                logger.info(
                    "Qwen TTS realtime started sessionId=%s traceId=%s roundId=%s",
                    session.session_id,
                    session.trace_id,
                    session.round_id,
                )
                continue

            if session is None:
                await _safe_send_text(ws, ws_error_msg(ErrorCode.TTS_FAIL, "session not started"))
                continue

            if msg_type == "text":
                content = data.get("content", "")
                session.touch()
                logger.info(
                    "Qwen TTS realtime text received sessionId=%s roundId=%s chars=%s",
                    session.session_id,
                    session.round_id,
                    len(content),
                )
                if tts_rt is not None:
                    await tts_rt.send_text(content)
                continue

            if msg_type == "finish":
                session.touch()
                logger.info(
                    "Qwen TTS realtime finish received sessionId=%s roundId=%s",
                    session.session_id,
                    session.round_id,
                )
                if tts_rt is not None:
                    await tts_rt.finish()
                    await tts_rt.send_tts_end("completed")
                await _safe_send_text(
                    ws,
                    json.dumps(
                        {
                            "type": "completed",
                            "sessionId": session.session_id,
                            "traceId": session.trace_id,
                            "roundId": session.round_id,
                            "provider": "qwen_realtime",
                        },
                        ensure_ascii=False,
                    )
                )
                logger.info(
                    "Qwen TTS realtime completed sessionId=%s roundId=%s",
                    session.session_id,
                    session.round_id,
                )
                if tts_rt is not None:
                    await tts_rt.close()
                    tts_rt = None
                session_service.release_tts()
                acquired = False
                session_service.detach_connection(session.session_id)
                session.close()
                session_service.remove(session.session_id)
                session = None
                continue

            if msg_type == "cancel":
                logger.info(
                    "Qwen TTS realtime cancel received sessionId=%s roundId=%s",
                    session.session_id,
                    session.round_id,
                )
                if tts_rt is not None:
                    await tts_rt.send_tts_end("cancelled")
                    await tts_rt.cancel()
                    await tts_rt.close()
                    tts_rt = None
                await _safe_send_text(
                    ws,
                    json.dumps(
                        {
                            "type": "cancelled",
                            "sessionId": session.session_id,
                            "traceId": session.trace_id,
                            "roundId": session.round_id,
                            "provider": "qwen_realtime",
                        },
                        ensure_ascii=False,
                    )
                )
                if acquired:
                    session_service.release_tts()
                    acquired = False
                session_service.detach_connection(session.session_id)
                session.close()
                session_service.remove(session.session_id)
                session = None
                continue

            logger.warning(
                "Qwen TTS realtime unsupported message sessionId=%s roundId=%s type=%s",
                session.session_id,
                session.round_id,
                msg_type,
            )
            await _safe_send_text(
                ws,
                ws_error_msg(
                    ErrorCode.TTS_FAIL,
                    f"unsupported message type: {msg_type}",
                    session_id=session.session_id,
                    trace_id=session.trace_id,
                )
            )
    except RelayError as exc:
        logger.warning("Qwen TTS realtime websocket error code=%s message=%s", exc.code, exc.message)
        if session is not None:
            await _safe_send_text(
                ws,
                ws_error_msg(
                    exc.code,
                    exc.message,
                    session_id=session.session_id,
                    trace_id=session.trace_id,
                )
            )
        else:
            await _safe_send_text(ws, ws_error_msg(exc.code, exc.message))
    except WebSocketDisconnect:
        return
    finally:
        _sid = session.session_id if session else "-"
        logger.info("Qwen TTS realtime ws closed sessionId=%s", _sid)
        if tts_rt is not None:
            await tts_rt.close()
        if acquired:
            session_service.release_tts()
        if session is not None:
            session_service.detach_connection(session.session_id)
            session.close()
            session_service.remove(session.session_id)
