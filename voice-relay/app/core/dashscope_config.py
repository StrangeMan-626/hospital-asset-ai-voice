import dashscope

from app.core.config import Settings


def configure_dashscope(settings: Settings) -> None:
    dashscope.api_key = settings.CLOUD_API_KEY
    dashscope.base_http_api_url = settings.dashscope_http_base_url
    dashscope.base_websocket_api_url = settings.dashscope_websocket_base_url
