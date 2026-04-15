import asyncio
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
START_IDLE_TIMEOUT_S = 5
PONG = json.dumps({"type": "pong"})


def _preview_text(value: str, limit: int = 200) -> str:
    value = value.replace("\r", "\\r").replace("\n", "\\n")
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


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
    audio_chunks = 0
    audio_bytes = 0
    receive_task = None
    stop_requested = False

    async def _finish_current_audio(reason: str) -> None:
        nonlocal stop_requested
        if session is None or stop_requested or audio_chunks <= 0:
            return
        stop_requested = True
        session.touch()
        logger.info(
            "ASR realtime auto stop sessionId=%s reason=%s chunks=%s bytes=%s degraded=%s",
            session.session_id,
            reason,
            audio_chunks,
            audio_bytes,
            degraded_mode,
        )
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

    try:
        while True:
            if receive_task is None:
                receive_task = asyncio.create_task(ws.receive())
            idle_timeout_s = None
            if session is None:
                idle_timeout_s = START_IDLE_TIMEOUT_S
            elif audio_chunks > 0 and not stop_requested:
                idle_timeout_s = max(settings.ASR_MAX_END_SILENCE_MS, 100) / 1000
            done, _ = await asyncio.wait({receive_task}, timeout=idle_timeout_s)
            if not done:
                if session is None:
                    logger.warning(
                        "ASR realtime start timeout ip=%s timeout=%ss",
                        client_ip,
                        START_IDLE_TIMEOUT_S,
                    )
                    await ws.close(code=1008, reason="start timeout")
                    disconnected = True
                    break
                await _finish_current_audio("audio_idle")
                continue
            msg = receive_task.result()
            receive_task = None
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
                    logger.warning(
                        "ASR realtime audio received before start ip=%s bytes=%s",
                        client_ip,
                        len(chunk),
                    )
                    await ws.send_text(ws_error_msg(ErrorCode.ASR_BAD_AUDIO, "session not started"))
                    continue
                session.touch()
                audio_chunks += 1
                audio_bytes += len(chunk)
                if audio_chunks == 1:
                    logger.info(
                        "ASR realtime first audio chunk sessionId=%s bytes=%s",
                        session.session_id,
                        len(chunk),
                    )
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
                logger.warning(
                    "ASR realtime invalid control json ip=%s text=%s",
                    client_ip,
                    _preview_text(text),
                )
                await ws.send_text(ws_error_msg(ErrorCode.ASR_BAD_AUDIO, "invalid json payload"))
                continue

            msg_type = data.get("type")
            logger.info(
                "ASR realtime control received ip=%s type=%s sessionId=%s terminalId=%s roundId=%s",
                client_ip,
                msg_type,
                data.get("sessionId", ""),
                data.get("terminalId", ""),
                data.get("roundId", ""),
            )
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
                requested_session_id = data.get("sessionId")
                existing = session_service.get(requested_session_id) if requested_session_id else None
                if existing is not None:
                    if existing.state == SessionState.SUSPENDED and not existing.is_expired(settings.SESSION_SUSPEND_TTL_MS):
                        session = existing
                        session.terminal_id = data.get("terminalId", session.terminal_id)
                        session.round_id = data.get("roundId", "")
                        session.resume()
                    else:
                        await ws.send_text(
                            ws_error_msg(
                                ErrorCode.ASR_SESSION_CLOSED,
                                f"session already exists in state {existing.state}",
                                session_id=existing.session_id,
                                trace_id=existing.trace_id,
                            )
                        )
                        session_service.release_asr()
                        acquired = False
                        continue
                else:
                    session = Session(requested_session_id, data.get("terminalId", ""))
                    session.round_id = data.get("roundId", "")
                    session_service.register(session)
                    session.activate()
                session_service.attach_connection(session.session_id, ws)
                stop_requested = False
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
                session.round_id = data.get("roundId", "")
                session.resume()
                session_service.attach_connection(session.session_id, ws)
                stop_requested = False
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
                    "ASR realtime resumed sessionId=%s traceId=%s roundId=%s degraded=%s",
                    session.session_id,
                    session.trace_id,
                    session.round_id,
                    degraded_mode,
                )
                await ws.send_text(json.dumps({"type": "resumed", "sessionId": session.session_id}, ensure_ascii=False))
                continue

            if session is None:
                logger.warning(
                    "ASR realtime control before start ip=%s type=%s payload=%s",
                    client_ip,
                    msg_type,
                    _preview_text(text),
                )
                await ws.send_text(ws_error_msg(ErrorCode.ASR_SESSION_CLOSED, "session not started"))
                continue

            if msg_type == "stop":
                logger.info(
                    "ASR realtime stop received sessionId=%s chunks=%s bytes=%s degraded=%s",
                    session.session_id,
                    audio_chunks,
                    audio_bytes,
                    degraded_mode,
                )
                await _finish_current_audio("client_stop")
                continue

            if msg_type == "speech_resume":
                session.touch()
                stop_requested = False
                if asr_rt is not None and not degraded_mode:
                    await asr_rt.speech_resume()
                continue

            logger.warning(
                "ASR realtime unsupported control type ip=%s sessionId=%s type=%s payload=%s",
                client_ip,
                session.session_id,
                msg_type,
                _preview_text(text),
            )
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
        if receive_task is not None and not receive_task.done():
            receive_task.cancel()
        _sid = session.session_id if session else "-"
        logger.info(
            "ASR realtime ws closed sessionId=%s disconnected=%s chunks=%s bytes=%s",
            _sid,
            disconnected,
            audio_chunks,
            audio_bytes,
        )
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
