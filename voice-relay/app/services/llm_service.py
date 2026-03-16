import json
import httpx
from fastapi import Response
from fastapi.responses import StreamingResponse
from app.core.config import get_settings
from app.core.http_client import get_client


async def chat_completions(raw_body: bytes) -> Response:
    settings = get_settings()
    url = f"{settings.llm_api_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }

    try:
        body = json.loads(raw_body)
        is_stream = body.get("stream", False)
    except (json.JSONDecodeError, KeyError):
        is_stream = False

    if is_stream:
        return await _stream_request(url, headers, raw_body, settings.LLM_TIMEOUT)
    else:
        return await _normal_request(url, headers, raw_body, settings.LLM_TIMEOUT)


async def _normal_request(url: str, headers: dict, body: bytes, timeout: int) -> Response:
    client = get_client()
    resp = await client.post(url, content=body, headers=headers, timeout=timeout)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


async def _stream_request(url: str, headers: dict, body: bytes, timeout: int) -> StreamingResponse:
    client = get_client()

    async def event_generator():
        async with client.stream("POST", url, content=body, headers=headers, timeout=timeout) as resp:
            async for chunk in resp.aiter_bytes():
                yield chunk

    return StreamingResponse(event_generator(), media_type="text/event-stream")
