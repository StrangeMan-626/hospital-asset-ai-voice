import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.config import get_settings
from app.core.errors import ErrorCode, RelayError, ws_error_msg
from app.core.security import is_ip_allowed
from app.models.session import Session, SessionState
from app.services.asr_realtime_service import ASRRealtimeSession

logger = logging.getLogger("voice-relay")
router = APIRouter(tags=["ASR"])

MAX_DEGRADE_AUDIO_BYTES = 320000
PONG = json.dumps({"type": "pong"})


@router.websocket("/asr/realtime")
async def asr_realtime(ws: WebSocket):
    client_ip = ws.client.host if ws.client else "0.0.0.0"
    if not is_ip_allowed(client_ip):
        await ws.close(code=1008, reason="forbidden")
        return

    await ws.accept()
    settings = get_settings()
    session_service = ws.app.state.session_service
    degrade_service = ws.app.state.degrade_service

    session: Session | None = None
    asr_rt: ASRRealtimeSession | None = None
    degraded_mode = False
    degraded_audio = bytearray()
    acquired = False
    disconnected = False

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                disconnected = True
                break
            if msg["type"] != "websocket.receive":
                continue

            if msg.get("bytes") is not None:
                chunk = msg.get("bytes") or b""
                if not chunk:
                    continue
                if session is None:
                    await ws.send_text(ws_error_msg(ErrorCode.ASR_BAD_AUDIO, "session not started"))
                    continue
                session.touch()
                if degraded_mode:
                    if len(degraded_audio) + len(chunk) > MAX_DEGRADE_AUDIO_BYTES:
                        await ws.send_text(
                            ws_error_msg(
                                ErrorCode.ASR_BAD_AUDIO,
                                "audio exceeds degrade buffer limit",
                                session_id=session.session_id,
                                trace_id=session.trace_id,
                            )
                        )
                        session.close()
                        break
                    degraded_audio.extend(chunk)
                elif asr_rt is not None:
                    try:
                        await asr_rt.feed_audio(chunk)
                    except RelayError as exc:
                        degrade_service.record_asr_fail()
                        await ws.send_text(
                            ws_error_msg(
                                exc.code,
                                exc.message,
                                session_id=session.session_id,
                                trace_id=session.trace_id,
                            )
                        )
                continue

            text = msg.get("text")
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                await ws.send_text(ws_error_msg(ErrorCode.ASR_BAD_AUDIO, "invalid json payload"))
                continue

            msg_type = data.get("type")
            if msg_type == "ping":
                await ws.send_text(PONG)
                continue

            if msg_type == "start":
                if session is not None and session.state != SessionState.CLOSED:
                    await ws.send_text(
                        ws_error_msg(
                            ErrorCode.ASR_SESSION_CLOSED,
                            "session already started",
                            session_id=session.session_id,
                            trace_id=session.trace_id,
                        )
                    )
                    continue
                if not acquired:
                    acquired = await session_service.acquire_asr()
                if not acquired:
                    await ws.send_text(ws_error_msg(ErrorCode.SESSION_LIMIT, "ASR session limit reached"))
                    continue
                session = Session(data.get("sessionId"), data.get("terminalId", ""))
                session_service.register(session)
                session_service.attach_connection(session.session_id, ws)
                session.activate()
                degraded_audio.clear()
                degraded_mode = degrade_service.should_degrade_asr()
                asr_rt = None
                if not degraded_mode:
                    asr_rt = ASRRealtimeSession(session, ws, settings)
                    try:
                        await asr_rt.start(data.get("hotwords"), data.get("maxEndSilenceMs"))
                        degrade_service.record_asr_success()
                    except RelayError as exc:
                        degrade_service.record_asr_fail()
                        await ws.send_text(
                            ws_error_msg(
                                exc.code,
                                exc.message,
                                session_id=session.session_id,
                                trace_id=session.trace_id,
                            )
                        )
                        await asr_rt.close()
                        asr_rt = None
                        degraded_mode = True
                continue

            if msg_type == "resume":
                requested_session_id = data.get("sessionId", "")
                found = session_service.get(requested_session_id)
                if found is None or found.state != SessionState.SUSPENDED or found.is_expired(settings.SESSION_SUSPEND_TTL_MS):
                    await ws.send_text(
                        ws_error_msg(ErrorCode.SESSION_EXPIRED, "session expired", session_id=requested_session_id)
                    )
                    continue
                if not acquired:
                    acquired = await session_service.acquire_asr()
                if not acquired:
                    await ws.send_text(
                        ws_error_msg(
                            ErrorCode.SESSION_LIMIT,
                            "ASR session limit reached",
                            session_id=found.session_id,
                            trace_id=found.trace_id,
                        )
                    )
                    continue
                session = found
                session.resume()
                session_service.attach_connection(session.session_id, ws)
                degraded_audio.clear()
                degraded_mode = degrade_service.should_degrade_asr()
                asr_rt = None
                if not degraded_mode:
                    asr_rt = ASRRealtimeSession(session, ws, settings)
                    try:
                        await asr_rt.start(data.get("hotwords"), data.get("maxEndSilenceMs"))
                        degrade_service.record_asr_success()
                    except RelayError as exc:
                        degrade_service.record_asr_fail()
                        await ws.send_text(
                            ws_error_msg(
                                exc.code,
                                exc.message,
                                session_id=session.session_id,
                                trace_id=session.trace_id,
                            )
                        )
                        await asr_rt.close()
                        asr_rt = None
                        degraded_mode = True
                await ws.send_text(json.dumps({"type": "resumed", "sessionId": session.session_id}, ensure_ascii=False))
                continue

            if session is None:
                await ws.send_text(ws_error_msg(ErrorCode.ASR_SESSION_CLOSED, "session not started"))
                continue

            if msg_type == "stop":
                session.touch()
                if degraded_mode:
                    result = await degrade_service.asr_sync_fallback(bytes(degraded_audio), filename="audio.pcm")
                    payload = {
                        "type": "final",
                        "text": result.get("text", ""),
                        "sessionId": session.session_id,
                        "traceId": session.trace_id,
                        "fallbackFromPartial": False,
                        "degraded": True,
                    }
                    await ws.send_text(json.dumps(payload, ensure_ascii=False))
                    degraded_audio.clear()
                    degrade_service.record_asr_success()
                elif asr_rt is not None:
                    await asr_rt.stop()
                continue

            if msg_type == "speech_resume":
                session.touch()
                if asr_rt is not None and not degraded_mode:
                    await asr_rt.speech_resume()
                continue

            await ws.send_text(
                ws_error_msg(
                    ErrorCode.ASR_BAD_AUDIO,
                    f"unsupported message type: {msg_type}",
                    session_id=session.session_id,
                    trace_id=session.trace_id,
                )
            )
    except WebSocketDisconnect:
        disconnected = True
    finally:
        if asr_rt is not None:
            await asr_rt.close()
        if acquired:
            session_service.release_asr()
        if session is not None:
            session_service.detach_connection(session.session_id)
            if disconnected and session.state == SessionState.ACTIVE:
                session.suspend()
            elif session.state != SessionState.SUSPENDED:
                session.close()
                session_service.remove(session.session_id)
