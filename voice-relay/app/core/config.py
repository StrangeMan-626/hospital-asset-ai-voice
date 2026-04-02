from functools import lru_cache

from pydantic_settings import BaseSettings


_REQUIRED_FIELDS = [
    "CLOUD_API_KEY",
    "ASR_REALTIME_MODEL",
    "ASR_SYNC_MODEL",
    "LLM_DEFAULT_MODEL",
    "TTS_REALTIME_MODEL",
    "TTS_SYNC_MODEL",
    "TTS_VOICE",
]


class Settings(BaseSettings):
    CLOUD_API_KEY: str = ""
    CLOUD_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DASHSCOPE_HTTP_BASE_URL: str = "https://dashscope.aliyuncs.com/api/v1"
    DASHSCOPE_WEBSOCKET_BASE_URL: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

    ASR_REALTIME_MODEL: str = ""
    ASR_SYNC_MODEL: str = ""
    ASR_TIMEOUT: int = 15
    ASR_FINAL_WAIT_MS: int = 1200
    ASR_MAX_END_SILENCE_MS: int = 600
    ASR_ENABLE_HOTWORDS: bool = True

    LLM_API_KEY: str = ""
    LLM_API_URL: str = ""
    LLM_DEFAULT_MODEL: str = ""
    LLM_TIMEOUT: int = 10

    TTS_REALTIME_MODEL: str = ""
    TTS_SYNC_MODEL: str = ""
    TTS_VOICE: str = ""
    TTS_AUDIO_FORMAT: str = "mp3"
    TTS_TIMEOUT: int = 15
    TTS_FIRST_CHUNK_TIMEOUT: int = 3

    SESSION_MAX_CONCURRENT: int = 50
    SESSION_SUSPEND_TTL_MS: int = 10000
    SESSION_HEARTBEAT_INTERVAL_MS: int = 15000
    SESSION_HEARTBEAT_TIMEOUT_MS: int = 30000

    DEGRADE_ENABLED: bool = True
    DEGRADE_RECOVER_INTERVAL_MS: int = 60000

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

    def validate(self) -> None:
        missing = [field for field in _REQUIRED_FIELDS if not getattr(self, field, "")]
        if missing:
            raise RuntimeError(f"缺失必填环境变量: {', '.join(missing)}")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate()
    return settings
