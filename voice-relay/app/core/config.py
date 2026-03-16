from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    CLOUD_API_KEY: str = ""
    CLOUD_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DASHSCOPE_HTTP_BASE_URL: str = "https://dashscope.aliyuncs.com/api/v1"
    DASHSCOPE_WEBSOCKET_BASE_URL: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

    ASR_MODEL: str = "paraformer-realtime-v2"
    ASR_TIMEOUT: int = 15

    LLM_API_KEY: str = ""
    LLM_API_URL: str = ""
    LLM_MODEL: str = "qwen-plus"
    LLM_TIMEOUT: int = 30

    TTS_MODEL: str = "cosyvoice-v3-flash"
    TTS_VOICE: str = "longanyang"
    TTS_TIMEOUT: int = 15

    PORT: int = 9000
    ALLOWED_IPS: str = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1"
    LOG_LEVEL: str = "INFO"

    @property
    def llm_api_key(self) -> str:
        return self.LLM_API_KEY or self.CLOUD_API_KEY

    @property
    def llm_api_url(self) -> str:
        return (self.LLM_API_URL or self.CLOUD_BASE_URL).rstrip("/")

    @property
    def dashscope_http_base_url(self) -> str:
        return self.DASHSCOPE_HTTP_BASE_URL.rstrip("/")

    @property
    def dashscope_websocket_base_url(self) -> str:
        return self.DASHSCOPE_WEBSOCKET_BASE_URL.rstrip("/")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
