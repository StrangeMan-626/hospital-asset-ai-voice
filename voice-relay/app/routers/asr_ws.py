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


def _resolve_vocabulary_id(data: dict) -> str | None:
    vocabulary_id = data.get("vocabularyId") or data.get("vocabulary_id")
    hotwords = data.get("hotwords")
    if hotwords and not vocabulary_id:
        logger.warning("deprecated ASR hotwords payload received without vocabularyId; ignoring hotwords")
    return vocabulary_id


def _resolve_max_sentence_silence_ms(data: dict) -> int | None:
    if data.get("maxSentenceSilenceMs") is not None:
        return data.get("maxSentenceSilenceMs")
    return data.get("maxEndSilenceMs")


@router.websocket("/asr/realtime")
async def asr_realtime(ws: WebSocket):
    client_ip = ws.client.host if ws.client else "0.0.0.0"
    if not is_ip_allowed(client_ip):
        await ws.close(code=1008, reason="forbidden")
        return

    await ws.accept()
    logger.info("ASR realtime ws connected ip=%s", client_ip)
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
                session.round_id = data.get("roundId", "")
                session_service.register(session)
                session_service.attach_connection(session.session_id, ws)
                session.activate()
                degraded_audio.clear()
                degraded_mode = degrade_service.should_degrade_asr()
                vocabulary_id = _resolve_vocabulary_id(data)
                max_sentence_silence_ms = _resolve_max_sentence_silence_ms(data)
                asr_rt = None
                if not degraded_mode:
                    asr_rt = ASRRealtimeSession(session, ws, settings)
                    try:
                        await asr_rt.start(vocabulary_id=vocabulary_id, max_sentence_silence_ms=max_sentence_silence_ms)
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
                logger.info(
                    "ASR realtime started sessionId=%s traceId=%s roundId=%s degraded=%s",
                    session.session_id,
                    session.trace_id,
                    session.round_id,
                    degraded_mode,
                )
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
                        "roundId": session.round_id,
                        "fallbackFromPartial": False,
                        "degraded": True,
                    }
                    await ws.send_text(json.dumps(payload, ensure_ascii=False))
                    degraded_audio.clear()
                    degrade_service.record_asr_success()
                elif asr_rt is not None:
                    await asr_rt.stop()
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
        _sid = session.session_id if session else "-"
        logger.info("ASR realtime ws closed sessionId=%s disconnected=%s", _sid, disconnected)
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
