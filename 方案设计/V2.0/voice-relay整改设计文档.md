# voice-relay V2.0 整改设计文档

## 1. 文档定位

本文基于以下上游文档，输出 `voice-relay` 项目的针对性整改方案：

- `方案设计/V2.0/AI语音实施方案.md`
- `方案设计/V2.0/AI语音V2.0开发文档.md`
- `方案设计/V2.0/9000模型服务项目改造说明.md`

本项目在整体架构中的定位，不是前端唤醒层，也不是业务问答编排层，而是 `9000` 模型网关/语音中继服务。V2.0 的整改目标，是把当前“同步代理”升级为“支持流式 ASR、流式 TTS、统一会话管理与降级能力的模型网关”。

本文只约束 `voice-relay` 仓库的整改范围，前端唤醒、`/ws/voice` 会话编排、规则优先/LLM 补充的业务逻辑仍由上游业务项目负责。

## 2. 当前现状

结合当前仓库代码，现状如下：

### 2.1 当前能力

- `app/routers/asr.py` 仅提供 `POST /asr/recognize`，通过上传整段音频完成一次性识别。
- `app/services/asr_service.py` 调用 DashScope SDK，输出单次识别文本，不支持 `partial/final`、句尾确认、热词动态透传和流式会话。
- `app/routers/tts.py` 仅提供 `POST /tts/synthesize`，返回完整 `audio/mpeg`。
- `app/services/tts_service.py` 同步合成整段音频，不支持首包优先、chunk 分片、取消合成。
- `app/routers/llm.py` 透传 `/v1/chat/completions`，支持流式 SSE，但没有模型默认值注入、统一 tracing、分场景超时控制。
- `app/core/http_client.py` 当前只有一个通用 `httpx.AsyncClient`，没有按 ASR/LLM/TTS 区分连接池和超时。
- `app/main.py` 只有 HTTP 路由，没有 WebSocket 入口、会话状态机、断线恢复、限流和熔断。

### 2.2 当前与 V2.0 的主要差距

对照上游方案，当前项目还缺少：

- `WS /asr/realtime`
- `WS /tts/realtime`
- `partial/final` 流式回推
- `speech_resume` / `stop` / `cancel` 等会话控制
- 会话生命周期管理与断线恢复
- 流式失败后的同步降级
- 统一错误码、`traceId`、会话级日志
- 按能力拆分的并发控制和超时策略

### 2.3 当前配置问题

`app/core/config.py` 虽然已接入 `.env`，但模型名仍在代码中提供了默认值：

- `ASR_MODEL = "paraformer-realtime-v2"`
- `LLM_MODEL = "qwen-plus"`
- `TTS_MODEL = "cosyvoice-v3-flash"`

这与本次要求“模型都要放到 env 进行配置”不一致。V2.0 必须改为：

- 模型名统一从 `.env` 读取
- 代码中不再写死默认模型名
- 启动时校验关键模型配置是否缺失

## 3. 整改目标

`voice-relay` 在 V2.0 的目标能力如下：

1. 保留现有同步接口作为兼容和降级入口：
   - `POST /asr/recognize`
   - `POST /v1/chat/completions`
   - `POST /tts/synthesize`
2. 新增流式接口：
   - `WS /asr/realtime`
   - `WS /tts/realtime`
3. 支持会话级状态管理：
   - `CREATED -> ACTIVE -> SUSPENDED -> CLOSED`
4. 支持流式控制消息：
   - `start`
   - `stop`
   - `speech_resume`
   - `resume`
   - `cancel`
   - `ping/pong`
5. 支持统一错误码、`sessionId`、`traceId`、性能指标记录。
6. 所有模型配置必须来自 `.env`，代码层不写死模型名称。

## 4. 整改边界

### 4.1 本项目负责

- 对上游业务服务提供 ASR/TTS/LLM 模型接入能力
- 封装 DashScope HTTP/WS 调用
- 提供流式 ASR/TTS 网关接口
- 提供同步接口兼容和自动降级
- 维护模型会话、并发控制、超时、错误码和日志

### 4.2 本项目不负责

- 前端本地唤醒词识别
- 前端本地唤醒应答音频
- `/ws/voice` 业务会话编排
- 规则优先/LLM 补充的意图识别策略
- 业务数据查询和模板答案生成

## 5. 目标架构

```text
业务服务(9001)
  ├─ WS /asr/realtime  -> voice-relay -> DashScope/模型ASR
  ├─ WS /tts/realtime  -> voice-relay -> DashScope/模型TTS
  └─ POST /v1/chat/completions -> voice-relay -> LLM兼容接口

voice-relay
  ├─ routers
  ├─ realtime session manager
  ├─ upstream clients
  ├─ degrade service
  └─ metrics / tracing / error mapping
```

核心原则：

- 流式链路优先
- 同步链路保留
- 模型配置外置到 `.env`
- 会话和异常处理统一收口

## 6. 下游对接方式

### 6.1 决策：DashScope SDK 异步流式模式

当前代码通过 `dashscope.audio.asr.Recognition` 和 `dashscope.audio.tts_v2.SpeechSynthesizer` 调用 ASR/TTS。V2.0 流式改造不需要抛弃 SDK 改写原生 WebSocket，而是切换到 SDK 已提供的异步流式接口：

- ASR：使用 `dashscope.audio.asr.Recognition` 的回调模式（`callback=on_event`），SDK 内部维护 WebSocket，应用层注册 `on_partial` / `on_final` / `on_error` 回调
- TTS：使用 `dashscope.audio.tts_v2.SpeechSynthesizer` 的流式模式（`callback=on_audio_chunk`），逐 chunk 回调

选择 SDK 而非原生 WebSocket 的原因：

- SDK 已封装认证、心跳、协议解析，减少出错面
- SDK 内部已处理 DashScope 的 WebSocket URL 拼接和鉴权
- 当前 `requirements.txt` 已包含 `dashscope`、`websockets` 依赖，无需额外引入
- 应用层只需要关注"收到 partial/final 后推给上游 WS"的桥接逻辑

LLM 继续使用 `httpx.AsyncClient` 透传到兼容 OpenAI 的 HTTP 接口，无需变更。

### 6.2 上下游连接关联规则

voice-relay 同时维护两层连接：上游（9001 → voice-relay）和下游（voice-relay → DashScope）。两层连接的生命周期必须明确关联：

**ASR 场景：**

| 上游事件 | 下游处理 |
| --- | --- |
| 上游发送 `start` | 创建下游 ASR SDK 实例，建立到 DashScope 的流式连接 |
| 上游发送 Binary Frame | 透传音频给下游 SDK |
| 上游发送 `stop` | 停止向下游送音频，等待下游返回最终 `final`（超时 `ASR_FINAL_WAIT_MS`） |
| 上游发送 `speech_resume` | 取消 final 等待倒计时，继续向下游送音频 |
| 上游 WS 断开，session 进入 SUSPENDED | 立即关闭下游 ASR 连接（DashScope 不支持 session 恢复） |
| 上游 `resume` 恢复 session | 新建下游 ASR SDK 实例（不是恢复旧连接） |
| 下游 ASR 连接异常断开 | 向上游推送 `error`，触发降级判断 |

**TTS 场景：**

| 上游事件 | 下游处理 |
| --- | --- |
| 上游发送 `start` | 创建下游 TTS SDK 实例 |
| 上游发送 `text` | 将文本送入下游 SDK |
| 上游发送 `finish` | 通知下游文本发送完毕，等待所有 chunk 回推后发送 `completed` |
| 上游发送 `cancel` | 立即关闭下游 TTS SDK 实例，丢弃缓冲区，返回 `cancelled` |
| 上游 WS 断开 | 立即关闭下游 TTS 连接，释放资源 |
| 下游 TTS 连接异常断开 | 向上游推送 `error`，触发降级判断 |

关键原则：**下游 DashScope 连接不支持恢复，每次 resume 或重连都是新建下游实例。** session 恢复的意义在于上游 9001 不需要重新走 `start` 初始化流程。

### 6.3 下游连接健康监测

- 下游 SDK 回调超过 `ASR_TIMEOUT` / `TTS_TIMEOUT` 无任何响应时，判定下游连接失效
- 失效后主动关闭下游连接，向上游推送 `error`
- 连续失效次数计入熔断计数器

## 7. 模块整改设计

### 7.1 配置层

重点修改文件：

- `voice-relay/app/core/config.py`
- `voice-relay/deploy/env.prod`
- 新增 `voice-relay/.env.example`

整改要求：

- 所有模型名改为必填环境变量。
- `Settings` 不再提供默认模型值。
- 启动时执行配置校验，缺失关键 env 时直接失败。
- 对 realtime/sync 场景分别配置模型与超时。

建议拆分后的配置：

- ASR：实时模型、同步模型、句尾等待、热词开关
- LLM：默认模型（网关只做透传，不区分意图/回答，具体模型由上游请求指定）、统一超时
- TTS：实时模型、同步模型、音色、音频格式、首包超时

### 7.2 ASR 流式能力

新增模块建议：

- `voice-relay/app/routers/asr_ws.py`
- `voice-relay/app/services/asr_realtime_service.py`
- `voice-relay/app/services/session_service.py`

接口设计：

- `WS /asr/realtime`

支持消息：

- `start`
- `stop`
- `speech_resume`
- `resume`

数据传输要求：

- 音频数据使用 WebSocket Binary Frame 直接传 PCM16
- 控制消息使用 Text Frame + JSON

服务端输出：

- `partial`
- `final`
- `resumed`
- `error`

实现要求：

- 支持 `sessionId`
- 支持 `terminalId`
- 支持 `hotwords`
- 支持 `maxEndSilenceMs`
- 收到 `stop` 后不是立即粗暴断流，而是等待最终 `final`
- 收到 `speech_resume` 后恢复当前会话

### 7.3 TTS 流式能力

新增模块建议：

- `voice-relay/app/routers/tts_ws.py`
- `voice-relay/app/services/tts_realtime_service.py`

接口设计：

- `WS /tts/realtime`

支持消息：

- `start`
- `text`
- `finish`
- `cancel`

输出要求：

- 音频 chunk 使用 Binary Frame
- 控制消息返回 `completed` / `cancelled` / `error`
- 支持首包优先返回

实现要求：

- 收到 `cancel` 后立刻终止当前合成
- 清理未发送 chunk
- 返回明确取消确认

### 7.4 LLM 兼容接口整改

重点修改文件：

- `voice-relay/app/services/llm_service.py`
- `voice-relay/app/core/http_client.py`

整改要求：

- 保留 OpenAI 兼容风格 `/v1/chat/completions`
- 不在代码中写死模型名
- 如果上游请求 body 中未传 `model`，由 `LLM_DEFAULT_MODEL` 注入
- 如果上游请求 body 中已传 `model`，原样透传，网关不干预
- 保留对 `stream/tools/tool_choice/temperature/max_tokens` 的原样透传
- 增加 `traceId`、`sessionId`、`terminalId` 透传与日志记录
- 超时统一使用 `LLM_TIMEOUT`，由上游业务服务自行区分意图/回答场景的调用策略

网关定位说明：

voice-relay 作为模型网关，不感知上游的业务语义（意图识别、答案生成等）。具体使用什么模型、什么超时策略是上游 9001 的责任。网关只负责：透传请求、注入默认模型、统一 tracing 和错误码。

### 7.5 会话管理与并发控制

新增模块建议：

- `voice-relay/app/services/session_service.py`
- `voice-relay/app/models/session.py`

状态机：

```text
CREATED -> ACTIVE -> SUSPENDED -> CLOSED
```

要求：

- WebSocket 断开后进入 `SUSPENDED`
- 保活窗口默认 `10s`
- 支持 `resume`（注意：resume 只恢复上游 session 状态，下游 DashScope 连接需要新建，见 6.2）
- 超过 TTL 自动关闭，同时释放下游连接
- 定期扫描僵尸 session（超过 `60s` 未活动强制关闭）

并发控制：

- ASR/TTS 的下游连接是每个 session 独占的有状态连接，不适用传统 HTTP 连接池复用模式
- 使用 `asyncio.Semaphore` 控制 ASR/TTS 并发 session 数上限（默认各 `50`）
- 超过上限时返回 `SESSION_LIMIT`
- 暴露 `active_asr_session_count` 和 `active_tts_session_count` 指标
- LLM 的 HTTP 连接池通过 `httpx.AsyncClient(limits=httpx.Limits(...))` 配置，与 ASR/TTS 独立

### 7.6 降级与恢复

新增模块建议：

- `voice-relay/app/services/degrade_service.py`

降级需要区分两种场景：

**场景一：连接建立阶段失败**

- 创建下游 ASR/TTS SDK 实例时连接失败
- 此时上游尚未推送音频，无数据丢失
- 直接切换到同步接口处理

ASR 降级：上游推送的音频分片在 voice-relay 内部缓冲，拼接后调用 `POST /asr/recognize`，返回结果标记 `degraded: true`。缓冲上限为 `maxSpeechMs` 对应的 PCM 大小（约 320KB/10s），超限则拒绝。

TTS 降级：收到全部文本后调用 `POST /tts/synthesize`，将完整音频切分为 chunk 逐个回推，标记 `degraded: true`。

**场景二：流式中途断流**

- 下游 ASR/TTS 连接在会话进行中异常断开
- 已推送的音频无法从下游恢复

ASR 中途断流：取最后一次 `partial` 结果作为最终文本，标记 `fallbackFromPartial: true`，向上游推送为 `final`。参考上游文档的 `fallbackFromPartial` 设计。

TTS 中途断流：向上游推送 `error`（`TTS_FAIL`），由上游决定是否降级重试。

**熔断与恢复：**

- 连续 `5` 次连接建立失败，触发熔断，后续请求直接走同步降级通道
- 熔断持续 `30s` 后进入半开状态，允许一次流式探活
- 探活成功恢复流式模式，失败继续熔断
- 降级后每 `60s` 尝试一次流式连接探活
- 降级和恢复事件必须记录日志

### 7.7 统一错误码与观测

重点修改文件：

- `voice-relay/app/core/errors.py`
- `voice-relay/app/core/logger.py`
- `voice-relay/app/main.py`

建议错误码：

- `ASR_CONNECT_FAIL`
- `ASR_TIMEOUT`
- `ASR_BAD_AUDIO`
- `ASR_SESSION_CLOSED`
- `TTS_CONNECT_FAIL`
- `TTS_TIMEOUT`
- `TTS_FAIL`
- `TTS_CANCELLED`
- `LLM_TIMEOUT`
- `LLM_BAD_RESPONSE`
- `SESSION_EXPIRED`
- `SESSION_LIMIT`
- `CONNECT_POOL_EXHAUSTED`
- `INTERNAL_ERROR`

必须记录的关键字段：

- `traceId`
- `sessionId`
- `terminalId`
- `asr_first_partial_ms`
- `asr_final_ms`
- `tts_first_chunk_ms`
- `tts_total_ms`
- `llm_cost_ms`
- `degrade_type`
- `reconnect_count`
- `session_state`

## 8. 配置设计

### 8.1 设计原则

本次整改强制要求：模型相关配置全部进入 `.env`，不允许在 Python 代码中写死默认模型。

落地原则如下：

- `config.py` 中保留字段定义，但不提供默认模型名
- `.env.example` 提供样例
- `deploy/env.prod` 提供生产值
- Docker 和本地运行统一通过 `env_file` 注入
- 启动时做 fail-fast 校验

### 8.2 建议环境变量

```env
# --- 基础配置 ---
PORT=9000
LOG_LEVEL=INFO
ALLOWED_IPS=127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16

# --- 平台接入 ---
CLOUD_API_KEY=
CLOUD_BASE_URL=
DASHSCOPE_HTTP_BASE_URL=
DASHSCOPE_WEBSOCKET_BASE_URL=

# --- ASR 配置 ---
ASR_REALTIME_MODEL=
ASR_SYNC_MODEL=
ASR_TIMEOUT=15
ASR_FINAL_WAIT_MS=1200
ASR_MAX_END_SILENCE_MS=600
ASR_ENABLE_HOTWORDS=true

# --- LLM 配置（网关只做透传，不区分业务场景）---
LLM_API_KEY=
LLM_API_URL=
LLM_DEFAULT_MODEL=
LLM_TIMEOUT=10

# --- TTS 配置 ---
TTS_REALTIME_MODEL=
TTS_SYNC_MODEL=
TTS_VOICE=
TTS_AUDIO_FORMAT=mp3
TTS_TIMEOUT=15
TTS_FIRST_CHUNK_TIMEOUT=3

# --- 会话与降级配置 ---
SESSION_MAX_CONCURRENT=50
SESSION_SUSPEND_TTL_MS=10000
SESSION_HEARTBEAT_INTERVAL_MS=15000
SESSION_HEARTBEAT_TIMEOUT_MS=30000
DEGRADE_ENABLED=true
DEGRADE_RECOVER_INTERVAL_MS=60000
```

### 8.3 对当前配置文件的直接要求

当前 `voice-relay/app/core/config.py` 需至少做以下调整：

- 删除 `ASR_MODEL`、`LLM_MODEL`、`TTS_MODEL` 的默认模型字符串
- 增加 realtime/sync 模型拆分字段（ASR、TTS 各有实时和同步模型）
- 增加 `LLM_DEFAULT_MODEL`（仅此一个，网关不区分意图/回答）
- 增加会话、降级、心跳相关字段

当前 `voice-relay/deploy/env.prod` 需至少做以下调整：

- 从单一 `ASR_MODEL` 升级为 `ASR_REALTIME_MODEL` + `ASR_SYNC_MODEL`
- 从单一 `LLM_MODEL` 升级为 `LLM_DEFAULT_MODEL`
- 从单一 `TTS_MODEL` 升级为 `TTS_REALTIME_MODEL` + `TTS_SYNC_MODEL`

## 9. 目录调整建议

建议整改后的目录结构如下：

```text
voice-relay/app
├─ core
│  ├─ config.py
│  ├─ errors.py
│  ├─ http_client.py
│  ├─ logger.py
│  └─ metrics.py
├─ models
│  └─ session.py
├─ routers
│  ├─ asr.py
│  ├─ asr_ws.py
│  ├─ llm.py
│  ├─ tts.py
│  └─ tts_ws.py
├─ services
│  ├─ asr_service.py
│  ├─ asr_realtime_service.py
│  ├─ llm_service.py
│  ├─ tts_service.py
│  ├─ tts_realtime_service.py
│  ├─ session_service.py
│  └─ degrade_service.py
└─ main.py
```

## 10. 分阶段实施建议

### P0a：配置改造（风险最低，先行）

- 重构 `config.py`：删除默认模型值，增加 realtime/sync 分层字段
- 增加 `.env.example`
- 更新 `deploy/env.prod`
- 启动时 fail-fast 校验关键配置
- 现有同步接口验证通过后，此阶段可独立上线

### P0b：WebSocket 路由骨架

- 新增 `asr_ws.py`、`tts_ws.py` 路由文件，实现 echo 级骨架
- 新增 `session_service.py` 基础框架
- 引入 `traceId` 生成和统一错误码结构
- 验证 FastAPI WebSocket 端点可连通

### P1：打通流式 ASR

- 实现 `WS /asr/realtime` 完整逻辑
- 接入 DashScope SDK 异步流式 ASR（见 6.1）
- 打通 Binary Frame 音频 → SDK → partial/final 回推
- 支持 `stop`（等待 final）、`speech_resume`（取消倒计时）
- session 状态机 CREATED → ACTIVE → CLOSED

### P2：打通流式 TTS

- 实现 `WS /tts/realtime` 完整逻辑
- 接入 DashScope SDK 流式 TTS
- 支持 `start/text/finish/cancel`
- 支持音频 chunk Binary Frame 回推
- 支持首包计时

### P3：完善稳定性

- 降级与熔断（区分连接失败和中途断流，见 7.6）
- 并发控制（Semaphore 限制 ASR/TTS 并发 session，见 7.5）
- 心跳和断线恢复（session SUSPENDED → resume → 新建下游连接，见 6.2）
- 优雅停机（见 11.3）
- 性能指标采集和日志

## 11. 验收标准

### 11.1 功能验收

1. 可以通过 `WS /asr/realtime` 接收 PCM Binary Frame 并回推 `partial/final`。
2. 可以通过 `WS /tts/realtime` 回推音频 chunk，并支持 `cancel`。
3. 收到 `stop` 后等待 `ASR_FINAL_WAIT_MS` 再关闭下游，不粗暴断流。
4. 收到 `speech_resume` 后取消关闭倒计时，继续接收音频。
5. 流式链路异常时可区分"连接失败"和"中途断流"两种降级路径。
6. 所有模型名均来自 `.env`，代码中不再写死默认模型。
7. LLM 接口当请求 body 未传 `model` 时注入 `LLM_DEFAULT_MODEL`，已传则原样透传。
8. 所有关键日志均可按 `traceId + sessionId` 串联。
9. 支持 `resume`、`heartbeat`、`SESSION_LIMIT` 等会话控制能力。
10. `resume` 后下游 DashScope 连接为新建，不依赖下游支持 session 恢复。

### 11.2 性能验收

voice-relay 作为中间层，自身引入的延迟必须控制在预算内（排除下游 DashScope 处理时间）：

| 指标 | 目标值 |
| --- | --- |
| ASR：收到上游音频帧到转发给下游 SDK | ≤ 5ms |
| ASR：收到下游 partial/final 到推给上游 | ≤ 10ms |
| TTS：收到下游音频 chunk 到推给上游 | ≤ 10ms |
| LLM：收到请求到开始转发给下游 | ≤ 20ms |
| session 创建耗时（含下游 SDK 实例化） | ≤ 200ms |
| 降级切换耗时 | ≤ 50ms |

配合上游整体目标：句尾到首文字 ≤ 800ms、句尾到首音频 ≤ 1500ms，voice-relay 的透传开销必须足够小，不能成为瓶颈。

### 11.3 优雅停机

服务重启/部署时：

- 收到 `SIGTERM` 后停止接受新 WebSocket 连接
- 等待现有 session 自然完成或超时（上限 `10s`）
- 超时后向所有活跃 session 推送 `error`（`INTERNAL_ERROR`），关闭上下游连接
- 在 `main.py` 的 `lifespan` shutdown 阶段执行

## 12. 本项目整改结论

`voice-relay` 的 V2.0 整改，本质上不是在现有同步 API 上做小修小补，而是把它升级为可承接上游 V2.0 主链路的流式模型网关。

本次整改最关键的约束：

- 流式 ASR / 流式 TTS 必须成为主能力，同步接口只保留为兼容与降级。
- 所有模型配置必须放到 `.env`，包括 ASR、TTS 的 realtime/sync 分层配置，禁止在代码中写死模型名。
- LLM 网关只做透传和默认模型注入，不感知上游业务语义。
- 下游 DashScope 连接不可恢复，resume 机制仅服务于上游 session 状态保持。
- voice-relay 自身延迟开销必须足够小，不能成为端到端链路的瓶颈。
