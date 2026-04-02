# voice-relay V2.0 开发文档

> 基于《voice-relay整改设计文档.md》，结合当前仓库代码现状，输出可直接落地的开发任务清单和代码变更说明。

## 1. 当前代码清单

```text
voice-relay/app
├─ core
│  ├─ config.py          # Settings(BaseSettings)，含默认模型名
│  ├─ dashscope_config.py # 设置 dashscope 全局变量
│  ├─ errors.py          # RelayError + 3 个 handler
│  ├─ http_client.py     # 单一 httpx.AsyncClient
│  └─ logger.py          # setup_logging + RequestLogMiddleware
├─ routers
│  ├─ asr.py             # POST /asr/recognize
│  ├─ llm.py             # POST /v1/chat/completions
│  └─ tts.py             # POST /tts/synthesize
├─ services
│  ├─ asr_service.py     # DashScope SDK 同步识别
│  ├─ llm_service.py     # httpx 透传 + SSE 流式
│  └─ tts_service.py     # DashScope SDK 同步合成
└─ main.py               # FastAPI app, lifespan, IP 白名单
```

## 2. 整改后目标目录

```text
voice-relay/app
├─ core
│  ├─ config.py          # [改] 删默认模型、加 realtime/sync 分层、加会话/降级配置、启动校验
│  ├─ dashscope_config.py # [不变]
│  ├─ errors.py          # [改] 增加统一错误码枚举，WS 错误消息格式
│  ├─ http_client.py     # [改] 拆分 LLM 专用 client，独立超时和连接池
│  ├─ logger.py          # [改] 增加 traceId/sessionId/terminalId 结构化字段
│  └─ metrics.py         # [新增] 性能指标采集
├─ models
│  └─ session.py         # [新增] Session 数据模型 + 状态枚举
├─ routers
│  ├─ asr.py             # [改] 模型名改从 config 读取（不再用 settings.ASR_MODEL）
│  ├─ asr_ws.py          # [新增] WS /asr/realtime
│  ├─ llm.py             # [改] 注入 LLM_DEFAULT_MODEL、traceId 日志
│  ├─ tts.py             # [改] 模型名改从 config 读取
│  └─ tts_ws.py          # [新增] WS /tts/realtime
├─ services
│  ├─ asr_service.py     # [改] 模型名从 config.ASR_SYNC_MODEL 读取
│  ├─ asr_realtime_service.py  # [新增] DashScope SDK 回调模式流式 ASR
│  ├─ llm_service.py     # [改] 默认模型注入、traceId 透传
│  ├─ tts_service.py     # [改] 模型名从 config.TTS_SYNC_MODEL 读取
│  ├─ tts_realtime_service.py  # [新增] DashScope SDK 流式 TTS
│  ├─ session_service.py # [新增] 会话状态机、并发控制、僵尸扫描
│  └─ degrade_service.py # [新增] 降级/熔断/半开探活
└─ main.py               # [改] 注册 WS 路由、lifespan 加入 session 清理和优雅停机
```

## 3. 分阶段任务清单

---

### P0a：配置改造

#### 3.1 config.py 改造

**文件**：`app/core/config.py`

**当前问题**：`ASR_MODEL`、`LLM_MODEL`、`TTS_MODEL` 都有默认值，不符合 V2.0 要求。

**变更内容**：

1. 删除 `ASR_MODEL`、`LLM_MODEL`、`TTS_MODEL` 三个字段
2. 新增以下字段（均不提供默认模型名，模型名字段使用空字符串并在启动校验中检查）：

```python
# ASR
ASR_REALTIME_MODEL: str = ""
ASR_SYNC_MODEL: str = ""
ASR_TIMEOUT: int = 15
ASR_FINAL_WAIT_MS: int = 1200
ASR_MAX_END_SILENCE_MS: int = 600
ASR_ENABLE_HOTWORDS: bool = True

# LLM
LLM_DEFAULT_MODEL: str = ""
LLM_TIMEOUT: int = 10

# TTS
TTS_REALTIME_MODEL: str = ""
TTS_SYNC_MODEL: str = ""
TTS_VOICE: str = ""
TTS_AUDIO_FORMAT: str = "mp3"
TTS_TIMEOUT: int = 15
TTS_FIRST_CHUNK_TIMEOUT: int = 3

# 会话
SESSION_MAX_CONCURRENT: int = 50
SESSION_SUSPEND_TTL_MS: int = 10000
SESSION_HEARTBEAT_INTERVAL_MS: int = 15000
SESSION_HEARTBEAT_TIMEOUT_MS: int = 30000

# 降级
DEGRADE_ENABLED: bool = True
DEGRADE_RECOVER_INTERVAL_MS: int = 60000
```

3. 新增启动校验方法：

```python
_REQUIRED_FIELDS = [
    "CLOUD_API_KEY",
    "ASR_REALTIME_MODEL", "ASR_SYNC_MODEL",
    "LLM_DEFAULT_MODEL",
    "TTS_REALTIME_MODEL", "TTS_SYNC_MODEL", "TTS_VOICE",
]

def validate(self) -> None:
    missing = [f for f in _REQUIRED_FIELDS if not getattr(self, f, "")]
    if missing:
        raise RuntimeError(f"缺失必填环境变量: {', '.join(missing)}")
```

4. 修改 `get_settings()`：调用 `validate()` 后再缓存

#### 3.2 .env.example 更新

**文件**：`voice-relay/.env.example`

替换为设计文档 8.2 节的完整模板，所有模型名字段留空。

#### 3.3 deploy/env.prod 更新

**文件**：`voice-relay/deploy/env.prod`

- `ASR_MODEL` → `ASR_REALTIME_MODEL` + `ASR_SYNC_MODEL`
- `LLM_MODEL` → `LLM_DEFAULT_MODEL`
- `TTS_MODEL` → `TTS_REALTIME_MODEL` + `TTS_SYNC_MODEL`
- 增加会话和降级配置项

#### 3.4 现有 service 适配

- `asr_service.py`：`settings.ASR_MODEL` → `settings.ASR_SYNC_MODEL`
- `tts_service.py`：`settings.TTS_MODEL` → `settings.TTS_SYNC_MODEL`，`settings.TTS_VOICE` 不变
- `llm_service.py`：`settings.LLM_MODEL` 删除引用（当前未直接使用，但需检查）

#### 3.5 main.py 调整

在 `lifespan` 的 startup 阶段加入：

```python
settings = get_settings()
settings.validate()
```

---

### P0b：WebSocket 路由骨架 + 基础设施

#### 3.6 统一错误码（errors.py）

**文件**：`app/core/errors.py`

新增错误码枚举：

```python
class ErrorCode:
    ASR_CONNECT_FAIL = "ASR_CONNECT_FAIL"
    ASR_TIMEOUT = "ASR_TIMEOUT"
    ASR_BAD_AUDIO = "ASR_BAD_AUDIO"
    ASR_SESSION_CLOSED = "ASR_SESSION_CLOSED"
    TTS_CONNECT_FAIL = "TTS_CONNECT_FAIL"
    TTS_TIMEOUT = "TTS_TIMEOUT"
    TTS_FAIL = "TTS_FAIL"
    TTS_CANCELLED = "TTS_CANCELLED"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_BAD_RESPONSE = "LLM_BAD_RESPONSE"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_LIMIT = "SESSION_LIMIT"
    CONNECT_POOL_EXHAUSTED = "CONNECT_POOL_EXHAUSTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
```

新增 WS 错误消息构造函数：

```python
def ws_error_msg(code: str, message: str, session_id: str = "", trace_id: str = "") -> str:
    return json.dumps({
        "type": "error",
        "code": code,
        "message": message,
        "sessionId": session_id,
        "traceId": trace_id,
    })
```

#### 3.7 Session 数据模型

**新文件**：`app/models/session.py`

```python
import enum
import time
import uuid

class SessionState(str, enum.Enum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"

class Session:
    def __init__(self, session_id: str | None = None, terminal_id: str = ""):
        self.session_id = session_id or str(uuid.uuid4())
        self.terminal_id = terminal_id
        self.trace_id = str(uuid.uuid4())
        self.state = SessionState.CREATED
        self.created_at = time.monotonic()
        self.last_active_at = self.created_at
        self.reconnect_count = 0

    def activate(self): ...
    def suspend(self): ...
    def resume(self): ...
    def close(self): ...
    def touch(self): ...
    def is_expired(self, ttl_ms: int) -> bool: ...
```

状态转换规则：
- `CREATED → ACTIVE`：收到 `start`
- `ACTIVE → SUSPENDED`：上游 WS 断开
- `SUSPENDED → ACTIVE`：收到 `resume` 且未超 TTL
- `SUSPENDED → CLOSED`：超过 `SESSION_SUSPEND_TTL_MS`
- `ACTIVE → CLOSED`：正常结束或异常
- 任意 → `CLOSED`：强制关闭

每次状态转换需记录日志，包含 `sessionId` + `traceId`。

#### 3.8 ASR WebSocket 路由骨架

**新文件**：`app/routers/asr_ws.py`

```python
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["ASR"])

@router.websocket("/asr/realtime")
async def asr_realtime(ws: WebSocket):
    await ws.accept()
    # P0b 阶段：echo 骨架
    # P1 阶段：接入 asr_realtime_service
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.receive":
                if "text" in msg:
                    await ws.send_text(msg["text"])
                elif "bytes" in msg:
                    await ws.send_text('{"type":"partial","text":"echo"}')
    except WebSocketDisconnect:
        pass
```

#### 3.9 TTS WebSocket 路由骨架

**新文件**：`app/routers/tts_ws.py`

结构同 `asr_ws.py`，路径 `/tts/realtime`。

#### 3.10 main.py 注册 WS 路由

```python
from app.routers import asr_ws, tts_ws

app.include_router(asr_ws.router)
app.include_router(tts_ws.router)
```

注意：WS 路由不经过 HTTP 中间件（`ip_whitelist`），需在 WS handler 内部做 IP 校验。

#### 3.11 traceId 生成

**文件**：`app/core/logger.py`

新增 `generate_trace_id()` 工具函数，格式 `relay-{uuid4_hex[:12]}`。

日志格式升级为：

```
%(asctime)s | %(levelname)s | traceId=%(traceId)s | %(message)s
```

使用 `logging.LoggerAdapter` 或 contextvars 注入 traceId。

#### 3.12 metrics.py

**新文件**：`app/core/metrics.py`

记录以下指标（内存计数器，暴露 `/metrics` 端点或日志输出）：

```python
class Metrics:
    asr_first_partial_ms: float
    asr_final_ms: float
    tts_first_chunk_ms: float
    tts_total_ms: float
    llm_cost_ms: float
    active_asr_sessions: int
    active_tts_sessions: int
```

---

### P1：流式 ASR

#### 3.13 asr_realtime_service.py

**新文件**：`app/services/asr_realtime_service.py`

核心类 `ASRRealtimeSession`：

```python
class ASRRealtimeSession:
    def __init__(self, session: Session, ws: WebSocket, settings: Settings): ...

    async def start(self, hotwords: list[str] | None = None, max_end_silence_ms: int | None = None):
        """创建 DashScope Recognition 实例，注册回调，启动流式连接"""

    async def feed_audio(self, pcm_data: bytes):
        """将上游 Binary Frame 透传给 SDK"""

    async def stop(self):
        """停止送音频，等待 final（超时 ASR_FINAL_WAIT_MS）"""

    async def speech_resume(self):
        """取消 final 等待倒计时，继续接收音频"""

    async def close(self):
        """关闭下游 SDK 连接，释放资源"""
```

**DashScope SDK 回调桥接**：

```python
def _on_event(self, result):
    sentence = result.get_sentence()
    if Recognition.is_sentence_end(sentence):
        # final
        asyncio.run_coroutine_threadsafe(
            self._ws.send_text(json.dumps({
                "type": "final",
                "text": sentence["text"],
                "sessionId": self._session.session_id,
                "traceId": self._session.trace_id,
            })),
            self._loop,
        )
    else:
        # partial
        asyncio.run_coroutine_threadsafe(
            self._ws.send_text(json.dumps({
                "type": "partial",
                "text": sentence["text"],
                "sessionId": self._session.session_id,
                "traceId": self._session.trace_id,
            })),
            self._loop,
        )

def _on_error(self, result):
    # 推送 error，触发降级
```

**注意事项**：

- DashScope SDK 的回调在 SDK 内部线程执行，必须用 `asyncio.run_coroutine_threadsafe` 桥接到 asyncio 事件循环
- 保存 `self._loop = asyncio.get_event_loop()` 在 `start()` 中获取
- `stop()` 实现：设置 flag 停止 `feed_audio`，启动 `asyncio.wait_for` 等待 final 事件，超时后取最后 partial 作为 final（`fallbackFromPartial: true`）
- `speech_resume()` 实现：取消 stop 计时器，清除 stop flag

#### 3.14 asr_ws.py 完整实现

**文件**：`app/routers/asr_ws.py`

消息协议：

**上游 → voice-relay（Text Frame，JSON）**：

```json
// start
{"type": "start", "sessionId": "可选", "terminalId": "T001", "hotwords": ["资产", "报修"], "maxEndSilenceMs": 800}

// stop
{"type": "stop"}

// speech_resume
{"type": "speech_resume"}

// resume
{"type": "resume", "sessionId": "之前的sessionId"}

// ping
{"type": "ping"}
```

**上游 → voice-relay（Binary Frame）**：PCM16 音频数据，直接透传

**voice-relay → 上游（Text Frame，JSON）**：

```json
// partial
{"type": "partial", "text": "你好", "sessionId": "xxx", "traceId": "xxx"}

// final
{"type": "final", "text": "你好世界", "sessionId": "xxx", "traceId": "xxx", "fallbackFromPartial": false}

// resumed
{"type": "resumed", "sessionId": "xxx"}

// error
{"type": "error", "code": "ASR_CONNECT_FAIL", "message": "...", "sessionId": "xxx", "traceId": "xxx"}

// pong
{"type": "pong"}
```

**处理流程**：

```python
@router.websocket("/asr/realtime")
async def asr_realtime(ws: WebSocket):
    await ws.accept()
    session = None
    asr_rt = None
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if "bytes" in msg and msg["bytes"]:
                if asr_rt:
                    await asr_rt.feed_audio(msg["bytes"])
                continue
            if "text" in msg:
                data = json.loads(msg["text"])
                msg_type = data.get("type")
                if msg_type == "start":
                    # 1. 检查并发限制（session_service.acquire_asr）
                    # 2. 创建 Session
                    # 3. 创建 ASRRealtimeSession
                    # 4. 调用 asr_rt.start()
                    # 5. 如果 start 失败，触发降级
                elif msg_type == "stop":
                    await asr_rt.stop()
                elif msg_type == "speech_resume":
                    await asr_rt.speech_resume()
                elif msg_type == "resume":
                    # 从 session_service 恢复 session
                    # 新建下游 ASR SDK 实例
                elif msg_type == "ping":
                    await ws.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        if session:
            session.suspend()
    finally:
        # 释放信号量、清理下游连接
```

---

### P2：流式 TTS

#### 3.15 tts_realtime_service.py

**新文件**：`app/services/tts_realtime_service.py`

核心类 `TTSRealtimeSession`：

```python
class TTSRealtimeSession:
    def __init__(self, session: Session, ws: WebSocket, settings: Settings): ...

    async def start(self):
        """创建 DashScope SpeechSynthesizer 实例，注册回调"""

    async def send_text(self, text: str):
        """将文本送入 SDK"""

    async def finish(self):
        """通知 SDK 文本发送完毕，等待所有 chunk 回推"""

    async def cancel(self):
        """立即关闭 SDK 实例，丢弃缓冲"""

    async def close(self):
        """释放资源"""
```

**DashScope SDK 回调桥接**：

```python
def _on_audio_chunk(self, data: bytes):
    if self._cancelled:
        return
    if not self._first_chunk_sent:
        self._first_chunk_ms = (time.monotonic() - self._start_time) * 1000
        self._first_chunk_sent = True
    asyncio.run_coroutine_threadsafe(
        self._ws.send_bytes(data),
        self._loop,
    )
```

#### 3.16 tts_ws.py 完整实现

**文件**：`app/routers/tts_ws.py`

消息协议：

**上游 → voice-relay（Text Frame，JSON）**：

```json
// start
{"type": "start", "sessionId": "可选", "terminalId": "T001"}

// text
{"type": "text", "content": "你好，请问有什么可以帮您？"}

// finish
{"type": "finish"}

// cancel
{"type": "cancel"}

// ping
{"type": "ping"}
```

**voice-relay → 上游（Binary Frame）**：音频 chunk

**voice-relay → 上游（Text Frame，JSON）**：

```json
// completed
{"type": "completed", "sessionId": "xxx", "traceId": "xxx"}

// cancelled
{"type": "cancelled", "sessionId": "xxx"}

// error
{"type": "error", "code": "TTS_FAIL", "message": "...", "sessionId": "xxx", "traceId": "xxx"}

// pong
{"type": "pong"}
```

---

### P3：稳定性

#### 3.17 session_service.py

**新文件**：`app/services/session_service.py`

```python
class SessionService:
    def __init__(self, settings: Settings):
        self._sessions: dict[str, Session] = {}
        self._asr_semaphore = asyncio.Semaphore(settings.SESSION_MAX_CONCURRENT)
        self._tts_semaphore = asyncio.Semaphore(settings.SESSION_MAX_CONCURRENT)
        self._suspend_ttl_ms = settings.SESSION_SUSPEND_TTL_MS

    async def acquire_asr(self) -> bool:
        """尝试获取 ASR 信号量，超时返回 False"""

    def release_asr(self): ...

    async def acquire_tts(self) -> bool: ...
    def release_tts(self): ...

    def register(self, session: Session): ...

    def get(self, session_id: str) -> Session | None: ...

    def remove(self, session_id: str): ...

    async def cleanup_loop(self):
        """每 5s 扫描一次，关闭超过 TTL 的 SUSPENDED session 和超过 60s 无活动的僵尸 session"""

    @property
    def active_asr_count(self) -> int: ...

    @property
    def active_tts_count(self) -> int: ...
```

生命周期：
- 在 `main.py` 的 `lifespan` startup 中创建 `SessionService` 实例，启动 `cleanup_loop` 后台任务
- shutdown 时取消 `cleanup_loop`，向所有活跃 session 推送 `INTERNAL_ERROR`

#### 3.18 degrade_service.py

**新文件**：`app/services/degrade_service.py`

```python
class DegradeService:
    def __init__(self, settings: Settings):
        self._asr_fail_count = 0
        self._tts_fail_count = 0
        self._asr_circuit_open = False
        self._tts_circuit_open = False
        self._circuit_open_time: float = 0
        self._threshold = 5
        self._circuit_timeout_s = 30
        self._recover_interval_ms = settings.DEGRADE_RECOVER_INTERVAL_MS
        self._enabled = settings.DEGRADE_ENABLED

    def record_asr_fail(self): ...
    def record_asr_success(self): ...
    def record_tts_fail(self): ...
    def record_tts_success(self): ...

    def should_degrade_asr(self) -> bool:
        """连续失败 >= 5 次 或 熔断中 返回 True"""

    def should_degrade_tts(self) -> bool: ...

    async def asr_sync_fallback(self, audio_buffer: bytes) -> dict:
        """调用 POST /asr/recognize 同步识别，返回结果标记 degraded: true"""

    async def tts_sync_fallback(self, text: str, ws: WebSocket):
        """调用 POST /tts/synthesize，切分 chunk 逐个回推，标记 degraded: true"""
```

熔断状态机：

```text
CLOSED（正常）
  └─ 连续失败 >= 5 → OPEN（熔断，直接降级）
                        └─ 30s 后 → HALF_OPEN（允许 1 次流式探活）
                                      ├─ 成功 → CLOSED
                                      └─ 失败 → OPEN
```

ASR 中途断流处理：
- 取最后一次 `partial` 作为 `final`，标记 `fallbackFromPartial: true`

TTS 中途断流处理：
- 推送 `error`（`TTS_FAIL`），由上游决定是否重试

#### 3.19 http_client.py 改造

**文件**：`app/core/http_client.py`

拆分为 LLM 专用 client：

```python
_llm_client: httpx.AsyncClient | None = None

async def start_client():
    global _llm_client
    settings = get_settings()
    _llm_client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.LLM_TIMEOUT),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        follow_redirects=True,
    )

def get_llm_client() -> httpx.AsyncClient:
    assert _llm_client is not None
    return _llm_client
```

#### 3.20 llm_service.py 改造

**文件**：`app/services/llm_service.py`

变更点：

1. 解析 body 后检查 `model` 字段：
   - 未传 → 注入 `settings.LLM_DEFAULT_MODEL`
   - 已传 → 原样透传
2. 生成 `traceId`，写入请求头 `X-Trace-Id`
3. 从请求中提取 `sessionId`、`terminalId`（如果有），记录到日志
4. 超时使用 `settings.LLM_TIMEOUT`

```python
async def chat_completions(raw_body: bytes, trace_id: str = "", session_id: str = "", terminal_id: str = "") -> Response:
    settings = get_settings()
    body = json.loads(raw_body)
    if "model" not in body or not body["model"]:
        body["model"] = settings.LLM_DEFAULT_MODEL
    # ... 透传逻辑
```

#### 3.21 优雅停机

**文件**：`app/main.py`

在 `lifespan` 的 shutdown 阶段：

```python
@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = get_settings()
    settings.validate()
    setup_logging(settings.LOG_LEVEL)
    await start_client()
    session_svc = SessionService(settings)
    cleanup_task = asyncio.create_task(session_svc.cleanup_loop())
    app.state.session_service = session_svc
    app.state.degrade_service = DegradeService(settings)
    logger.info("voice-relay started on port %d", settings.PORT)
    yield
    # 停止接受新连接已由 uvicorn 处理
    # 等待现有 session 完成，上限 10s
    cleanup_task.cancel()
    await session_svc.shutdown(timeout_s=10)
    await stop_client()
    logger.info("voice-relay stopped")
```

#### 3.22 WS 层 IP 校验

WebSocket 不经过 HTTP 中间件，需在 `accept()` 前校验：

```python
async def asr_realtime(ws: WebSocket):
    if not check_ip(ws.client.host):
        await ws.close(code=1008, reason="forbidden")
        return
    await ws.accept()
```

提取 `_get_allowed_nets` 和 IP 检查逻辑为公共函数，供 HTTP 中间件和 WS handler 共用。

## 4. 配置文件变更参考

### 4.1 .env.example

```env
# --- 基础配置 ---
PORT=9000
LOG_LEVEL=INFO
ALLOWED_IPS=127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16

# --- 平台接入 ---
CLOUD_API_KEY=
CLOUD_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_HTTP_BASE_URL=https://dashscope.aliyuncs.com/api/v1
DASHSCOPE_WEBSOCKET_BASE_URL=wss://dashscope.aliyuncs.com/api-ws/v1/inference

# --- ASR ---
ASR_REALTIME_MODEL=
ASR_SYNC_MODEL=
ASR_TIMEOUT=15
ASR_FINAL_WAIT_MS=1200
ASR_MAX_END_SILENCE_MS=600
ASR_ENABLE_HOTWORDS=true

# --- LLM ---
LLM_API_KEY=
LLM_API_URL=
LLM_DEFAULT_MODEL=
LLM_TIMEOUT=10

# --- TTS ---
TTS_REALTIME_MODEL=
TTS_SYNC_MODEL=
TTS_VOICE=
TTS_AUDIO_FORMAT=mp3
TTS_TIMEOUT=15
TTS_FIRST_CHUNK_TIMEOUT=3

# --- 会话与降级 ---
SESSION_MAX_CONCURRENT=50
SESSION_SUSPEND_TTL_MS=10000
SESSION_HEARTBEAT_INTERVAL_MS=15000
SESSION_HEARTBEAT_TIMEOUT_MS=30000
DEGRADE_ENABLED=true
DEGRADE_RECOVER_INTERVAL_MS=60000
```

### 4.2 deploy/env.prod 变更

```env
# ASR
ASR_REALTIME_MODEL=paraformer-realtime-v2
ASR_SYNC_MODEL=paraformer-realtime-v2

# LLM
LLM_DEFAULT_MODEL=qwen-plus

# TTS
TTS_REALTIME_MODEL=cosyvoice-v3-flash
TTS_SYNC_MODEL=cosyvoice-v3-flash
TTS_VOICE=longanyang
```

## 5. 接口汇总

| 接口 | 类型 | 阶段 | 状态 |
| --- | --- | --- | --- |
| `POST /asr/recognize` | HTTP | 已有 | 改：模型名适配 |
| `POST /v1/chat/completions` | HTTP | 已有 | 改：默认模型注入 + traceId |
| `POST /tts/synthesize` | HTTP | 已有 | 改：模型名适配 |
| `WS /asr/realtime` | WebSocket | P0b骨架/P1完整 | 新增 |
| `WS /tts/realtime` | WebSocket | P0b骨架/P2完整 | 新增 |
| `GET /health` | HTTP | 已有 | 不变 |

## 6. 文件变更清单

| 文件 | 操作 | 阶段 |
| --- | --- | --- |
| `app/core/config.py` | 改 | P0a |
| `.env.example` | 改 | P0a |
| `deploy/env.prod` | 改 | P0a |
| `deploy/.env.test` | 改 | P0a |
| `app/services/asr_service.py` | 改 | P0a |
| `app/services/tts_service.py` | 改 | P0a |
| `app/main.py` | 改 | P0a/P0b/P3 |
| `app/core/errors.py` | 改 | P0b |
| `app/core/logger.py` | 改 | P0b |
| `app/core/metrics.py` | 新增 | P0b |
| `app/models/session.py` | 新增 | P0b |
| `app/routers/asr_ws.py` | 新增 | P0b/P1 |
| `app/routers/tts_ws.py` | 新增 | P0b/P2 |
| `app/services/asr_realtime_service.py` | 新增 | P1 |
| `app/services/tts_realtime_service.py` | 新增 | P2 |
| `app/services/session_service.py` | 新增 | P3 |
| `app/services/degrade_service.py` | 新增 | P3 |
| `app/core/http_client.py` | 改 | P3 |
| `app/services/llm_service.py` | 改 | P3 |
| `app/routers/llm.py` | 改 | P3 |

## 7. 关键实现注意事项

1. **SDK 回调线程安全**：DashScope SDK 回调在内部线程执行，必须用 `asyncio.run_coroutine_threadsafe()` 将 WS 发送操作调度回主事件循环，不能直接 `await`。

2. **下游连接不可恢复**：DashScope 不支持 session 恢复，每次 `resume` 都是新建下游 SDK 实例。`resume` 只保持上游 session 元数据。

3. **Binary/Text Frame 区分**：音频数据走 Binary Frame，控制消息走 Text Frame + JSON。路由层通过 `msg["type"]` 区分。

4. **stop 不是断流**：收到 `stop` 后停止送音频，但要等 `ASR_FINAL_WAIT_MS` 让下游返回最终 final，不能直接关连接。

5. **降级缓冲上限**：ASR 降级时缓冲上限 = `maxSpeechMs` 对应的 PCM 大小（16000Hz × 2Byte × 10s ≈ 320KB），超限拒绝。

6. **并发控制粒度**：ASR/TTS 各自独立 Semaphore，LLM 走 httpx 连接池，三者互不影响。

7. **WS IP 校验**：WebSocket 不经过 HTTP 中间件，必须在 handler 内单独校验。
