import httpx

_client: httpx.AsyncClient | None = None


async def start_client():
    global _client
    _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True)


async def stop_client():
    global _client
    if _client:
        await _client.aclose()
        _client = None


def get_client() -> httpx.AsyncClient:
    assert _client is not None, "HTTP client not initialized"
    return _client
