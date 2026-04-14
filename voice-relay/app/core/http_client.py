import httpx

from app.core.config import get_settings

_llm_client: httpx.AsyncClient | None = None


async def start_client() -> None:
    global _llm_client
    settings = get_settings()
    _llm_client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.LLM_TIMEOUT),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        follow_redirects=True,
    )


async def stop_client() -> None:
    global _llm_client
    if _llm_client is not None:
        await _llm_client.aclose()
        _llm_client = None


def get_llm_client() -> httpx.AsyncClient:
    if _llm_client is None:
        raise RuntimeError("LLM HTTP client not initialized")
    return _llm_client
