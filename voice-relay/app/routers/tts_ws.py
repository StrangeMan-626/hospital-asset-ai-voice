import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.config import get_settings
from app.core.errors import ErrorCode, RelayError, ws_error_msg
from app.core.security import is_ip_allowed
from app.models.session import Session
from app.services.tts_realtime_service import TTSRealtimeSession

router = APIRouter(tags=["TTS"])
PONG = json.dumps({"type": "pong"})


@router.websocket("/tts/realtime")
async def tts_realtime(ws: WebSocket):
    client_ip = ws.client.host if ws.client else "0.0.0.0"
    if not is_ip_allowed(client_ip):
        await ws.close(code=1008, reason="forbidden")
        return

    await ws.accept()
    settings = get_settings()
    session_service = ws.app.state.session_service
    degrade_service = ws.app.state.degrade_service

    session: Session | None = None
    tts_rt: TTSRealtimeSession | None = None
    text_buffer: list[str] = []
    degraded_mode = False
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
                await ws.send_text(ws_error_msg(ErrorCode.TTS_FAIL, "invalid json payload"))
                continue

            msg_type = data.get("type")
            if msg_type == "ping":
                await ws.send_text(PONG)
                continue

            if msg_type == "start":
                if session is not None:
                    await ws.send_text(
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
                    await ws.send_text(ws_error_msg(ErrorCode.SESSION_LIMIT, "TTS session limit reached"))
                    continue
                session = Session(data.get("sessionId"), data.get("terminalId", ""))
                session_service.register(session)
                session_service.attach_connection(session.session_id, ws)
                session.activate()
                text_buffer.clear()
                degraded_mode = degrade_service.should_degrade_tts()
                if not degraded_mode:
                    tts_rt = TTSRealtimeSession(session, ws, settings)
                    try:
                        await tts_rt.start()
                        degrade_service.record_tts_success()
                    except RelayError as exc:
                        degrade_service.record_tts_fail()
                        await ws.send_text(
                            ws_error_msg(
                                exc.code,
                                exc.message,
                                session_id=session.session_id,
                                trace_id=session.trace_id,
                            )
                        )
                        await tts_rt.close()
                        tts_rt = None
                        degraded_mode = True
                continue

            if session is None:
                await ws.send_text(ws_error_msg(ErrorCode.TTS_FAIL, "session not started"))
                continue

            if msg_type == "text":
                content = data.get("content", "")
                session.touch()
                if degraded_mode:
                    text_buffer.append(content)
                elif tts_rt is not None:
                    try:
                        await tts_rt.send_text(content)
                    except RelayError as exc:
                        degrade_service.record_tts_fail()
                        await ws.send_text(
                            ws_error_msg(
                                exc.code,
                                exc.message,
                                session_id=session.session_id,
                                trace_id=session.trace_id,
                            )
                        )
                continue

            if msg_type == "finish":
                session.touch()
                if degraded_mode:
                    await degrade_service.tts_sync_fallback(
                        "".join(text_buffer),
                        ws,
                        session_id=session.session_id,
                        trace_id=session.trace_id,
                    )
                    degrade_service.record_tts_success()
                    payload = {
                        "type": "completed",
                        "sessionId": session.session_id,
                        "traceId": session.trace_id,
                        "degraded": True,
                    }
                elif tts_rt is not None:
                    await tts_rt.finish()
                    degrade_service.record_tts_success()
                    payload = {
                        "type": "completed",
                        "sessionId": session.session_id,
                        "traceId": session.trace_id,
                    }
                else:
                    payload = {"type": "completed", "sessionId": session.session_id, "traceId": session.trace_id}
                await ws.send_text(json.dumps(payload, ensure_ascii=False))
                if tts_rt is not None:
                    await tts_rt.close()
                    tts_rt = None
                session_service.release_tts()
                acquired = False
                session_service.detach_connection(session.session_id)
                session.close()
                session_service.remove(session.session_id)
                session = None
                text_buffer.clear()
                degraded_mode = False
                continue

            if msg_type == "cancel":
                if tts_rt is not None:
                    await tts_rt.cancel()
                    await tts_rt.close()
                    tts_rt = None
                text_buffer.clear()
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "cancelled",
                            "sessionId": session.session_id,
                            "traceId": session.trace_id,
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
                degraded_mode = False
                continue

            await ws.send_text(
                ws_error_msg(
                    ErrorCode.TTS_FAIL,
                    f"unsupported message type: {msg_type}",
                    session_id=session.session_id,
                    trace_id=session.trace_id,
                )
            )
    except WebSocketDisconnect:
        return
    finally:
        if tts_rt is not None:
            await tts_rt.close()
        if acquired:
            session_service.release_tts()
        if session is not None:
            session_service.detach_connection(session.session_id)
            session.close()
            session_service.remove(session.session_id)
