import json
import logging
import time

from fastapi import Response
from fastapi.responses import StreamingResponse

from app.core.config import get_settings
from app.core.http_client import get_llm_client
from app.core.logger import generate_trace_id, logging_context
from app.core.metrics import metrics

logger = logging.getLogger("voice-relay")


async def chat_completions(
    raw_body: bytes,
    trace_id: str = "",
    session_id: str = "",
    terminal_id: str = "",
) -> Response:
    settings = get_settings()
    body_bytes = raw_body
    resolved_trace_id = trace_id or generate_trace_id()
    resolved_session_id = session_id
    resolved_terminal_id = terminal_id
    is_stream = False

    try:
        body = json.loads(raw_body)
        if "model" not in body or not body["model"]:
            body["model"] = settings.LLM_DEFAULT_MODEL
        resolved_session_id = resolved_session_id or body.get("sessionId", "")
        resolved_terminal_id = resolved_terminal_id or body.get("terminalId", "")
        is_stream = bool(body.get("stream", False))
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    except (json.JSONDecodeError, TypeError):
        body = None

    url = f"{settings.llm_api_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
        "X-Trace-Id": resolved_trace_id,
    }

    with logging_context(
        trace_id=resolved_trace_id,
        session_id=resolved_session_id,
        terminal_id=resolved_terminal_id,
    ):
        logger.info("LLM request start stream=%s", is_stream)
        started = time.perf_counter()
        if is_stream:
            response = await _stream_request(url, headers, body_bytes, settings.LLM_TIMEOUT, resolved_trace_id)
        else:
            response = await _normal_request(url, headers, body_bytes, settings.LLM_TIMEOUT, resolved_trace_id)
        metrics.record("llm_cost_ms", (time.perf_counter() - started) * 1000)
        logger.info("LLM request finished")
        return response


async def _normal_request(
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: int,
    trace_id: str,
) -> Response:
    client = get_llm_client()
    resp = await client.post(url, content=body, headers=headers, timeout=timeout)
    response = Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )
    response.headers["X-Trace-Id"] = trace_id
    return response


async def _stream_request(
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: int,
    trace_id: str,
) -> StreamingResponse:
    client = get_llm_client()
    req = client.build_request("POST", url, content=body, headers=headers)
    resp = await client.send(req, stream=True)

    async def event_generator():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    response = StreamingResponse(
        event_generator(),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "text/event-stream"),
    )
    response.headers["X-Trace-Id"] = trace_id
    return response
