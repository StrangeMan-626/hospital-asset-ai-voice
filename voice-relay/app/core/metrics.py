import threading


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.asr_first_partial_ms: float = 0.0
        self.asr_final_ms: float = 0.0
        self.tts_first_chunk_ms: float = 0.0
        self.tts_total_ms: float = 0.0
        self.llm_cost_ms: float = 0.0
        self.active_asr_sessions: int = 0
        self.active_tts_sessions: int = 0

    def record(self, name: str, value: float) -> None:
        with self._lock:
            setattr(self, name, float(value))

    def set_active_asr_sessions(self, value: int) -> None:
        with self._lock:
            self.active_asr_sessions = value

    def set_active_tts_sessions(self, value: int) -> None:
        with self._lock:
            self.active_tts_sessions = value

    def snapshot(self) -> dict[str, float | int]:
        with self._lock:
            return {
                "asr_first_partial_ms": self.asr_first_partial_ms,
                "asr_final_ms": self.asr_final_ms,
                "tts_first_chunk_ms": self.tts_first_chunk_ms,
                "tts_total_ms": self.tts_total_ms,
                "llm_cost_ms": self.llm_cost_ms,
                "active_asr_sessions": self.active_asr_sessions,
                "active_tts_sessions": self.active_tts_sessions,
            }


metrics = Metrics()
