# voice-relay V2.1 低延迟实时版整改方案

## 1. 文档定位

本文基于上游文档 `AI语音V2.1低延迟实时版整改方案.md`，输出 `voice-relay` 项目的针对性整改方案。

voice-relay 在整体架构中的定位是 **模型网关/语音中继服务**，位于业务服务（9001 Java 后端）与 DashScope 模型服务之间。V2.1 的整改目标，是配合上游将整条语音链路从"整段串行"升级为"低延迟实时流式"，同时在中继层面补齐协议元信息、优化时序参数、强化音频格式一致性。

本文只约束 `voice-relay` 仓库的整改范围，前端采集/播放、Java 后端会话编排、意图识别/答案生成等仍由各自项目负责。

## 2. 当前现状

### 2.1 V2.0 已完成能力

经过 V2.0 整改，voice-relay 已具备以下能力：

| 能力 | 状态 | 对应模块 |
| --- | --- | --- |
| 实时 ASR WebSocket 接口 | ✅ 已完成 | `routers/asr_ws.py` + `services/asr_realtime_service.py` |
| 实时 TTS WebSocket 接口 | ✅ 已完成 | `routers/tts_ws.py` + `services/tts_realtime_service.py` |
| 同步 ASR/TTS 保留为降级入口 | ✅ 已完成 | `routers/asr.py` + `routers/tts.py` |
| 会话状态机 CREATED→ACTIVE→SUSPENDED→CLOSED | ✅ 已完成 | `models/session.py` |
| 断线重连 resume | ✅ 已完成 | `routers/asr_ws.py` |
| 降级熔断（连续失败→熔断→半开探活） | ✅ 已完成 | `services/degrade_service.py` |
| 并发控制（Semaphore 限制 ASR/TTS session 数） | ✅ 已完成 | `services/session_service.py` |
| speech_resume 取消句尾倒计时 | ✅ 已完成 | `services/asr_realtime_service.py` |
| PCM 格式输出支持 | ✅ 已完成 | `TTS_AUDIO_FORMAT=pcm` |
| 性能指标采集 | ✅ 已完成 | `core/metrics.py` |
| 模型配置外置到 .env | ✅ 已完成 | `core/config.py` |
| 优雅停机 | ✅ 已完成 | `services/session_service.py` |
| 唤醒词检测 | ✅ 已完成 | `routers/wakeup_ws.py` + `services/wakeup_service.py` |

### 2.2 与 V2.1 要求的差距

对照上游 V2.1 方案，voice-relay 尚未实现以下能力：

#### 2.2.1 TTS 尚未下发 codec 元信息

当前 TTS 实时链路只发送原始二进制音频 chunk 和 `completed` 消息，尚未在音频前后发送 `tts_start`（含 codec、sampleRate、channels）和 `tts_end`。V2.1 要求补齐这两个消息，使上游可在播放前获知编码格式。

#### 2.2.2 ASR 句尾参数尚未调优

当前配置：

| 参数 | 当前值 | V2.1 推荐值 |
| --- | --- | --- |
| `ASR_FINAL_WAIT_MS` | `15000` | `200` |
| `ASR_MAX_END_SILENCE_MS` | `600` | `300` |

这些参数均可通过配置直接调整，尚未按 V2.1 推荐值进行调优。

#### 2.2.3 尚未支持 roundId 透传

V2.1 引入 `roundId` 概念：一个 session 内可连续处理多轮 round。当前 relay 的 ASR 和 TTS session 尚未解析和透传 roundId，需要补齐以支持链路追踪和前端多轮音频隔离。

#### 2.2.4 TTS 降级链路尚未附带 codec 元信息

当前 `degrade_service.tts_sync_fallback()` 直接将整段音频按 4096 字节切片发送，尚未附带 `tts_start` / `tts_end` 元信息。需要补齐以保持与实时链路的协议一致性。

#### 2.2.5 ASR partial 尚未包含 stable 字段

V2.1 协议中 `partial_asr` 需要包含 `stable` 字段，表示当前 partial 是否为稳定中间结果。当前 relay 的 `partial` 消息尚未输出此字段，需要从 DashScope 回调中提取并补充。

## 3. V2.1 整改目标

voice-relay 在 V2.1 的整改目标：

1. **TTS 协议升级**：输出链路增加 `tts_start` / `tts_end` 元信息消息，明确下行音频的 `codec`、`sampleRate`、`channels`。
2. **ASR 时序优化**：调整句尾判定参数，将 final 等待时间从秒级降至毫秒级。
3. **roundId 透传**：ASR 和 TTS 链路支持 `roundId` 的接收、存储和响应回传。
4. **降级链路 codec 补齐**：降级模式下同样发送 codec 元信息，保证上游播放器行为一致。
5. **ASR partial 补充 stable 字段**：从 DashScope 回调中提取并回传。
6. **配置参数调优**：默认配置对齐 V2.1 推荐值。

### 3.1 非目标

以下内容不在 voice-relay V2.1 整改范围内：

- `/ws/voice` 业务会话编排（由 9001 Java 后端负责）
- 前端 AudioWorklet 采集与 VAD（由前端项目负责）
- 意图识别、工具路由、答案生成（由 9001 Java 后端负责）
- 多轮 round 状态机管理（由 9001 Java 后端负责）
- 前端播放器 codec 感知改造（由前端项目负责）

## 4. 模块整改设计

### 4.1 TTS 协议升级

#### 4.1.1 目标

在 TTS 实时链路中，音频数据前后增加结构化元信息消息，使上游能够：

- 在收到音频前获知编码格式和采样率
- 按正确的 codec 初始化播放器
- 明确知道一轮 TTS 的开始与结束

#### 4.1.2 新增 `tts_start` 消息

TTS 首个音频 chunk 到来时，在转发音频之前先发送：

```json
{
  "type": "tts_start",
  "sessionId": "xxx",
  "traceId": "xxx",
  "roundId": "r001",
  "codec": "pcm_s16le",
  "sampleRate": 16000,
  "channels": 1
}
```

字段说明：

| 字段 | 来源 |
| --- | --- |
| `codec` | 根据 `TTS_AUDIO_FORMAT` 配置映射：`pcm` → `pcm_s16le`，`wav` → `wav`，`mp3` → `audio/mpeg` |
| `sampleRate` | 根据 `TTS_AUDIO_FORMAT` 配置映射：`pcm`/`wav` → `16000`，`mp3` → `24000` |
| `channels` | 固定 `1`（mono） |
| `roundId` | 从上游 `start` 消息中透传 |

#### 4.1.3 新增 `tts_end` 消息

TTS 所有音频 chunk 发送完毕后、`completed` 之前发送：

```json
{
  "type": "tts_end",
  "sessionId": "xxx",
  "traceId": "xxx",
  "roundId": "r001",
  "reason": "completed"
}
```

`reason` 取值：`completed`（正常完成）、`cancelled`（被取消）、`error`（异常中断）。

#### 4.1.4 改造影响模块

| 文件 | 改造内容 |
| --- | --- |
| `services/tts_realtime_service.py` | `handle_audio_chunk()` 首包时先发送 `tts_start` JSON；新增 `handle_complete()` 中发送 `tts_end` |
| `routers/tts_ws.py` | `start` 消息解析 `roundId`；`finish` 和 `cancel` 流程中确保 `tts_end` 已发送 |
| `services/degrade_service.py` | `tts_sync_fallback()` 发送音频前先发送 `tts_start`，发送完后发送 `tts_end` |

### 4.2 ASR 时序参数优化

#### 4.2.1 配置调整

| 参数 | 当前值 | V2.1 目标值 | 说明 |
| --- | --- | --- | --- |
| `ASR_FINAL_WAIT_MS` | `15000` | `200` | `stop` 后等待 final 的超时时间 |
| `ASR_MAX_END_SILENCE_MS` | `600` | `300` | DashScope 模型侧的句尾静音判定阈值 |

#### 4.2.2 改造说明

- `ASR_FINAL_WAIT_MS` 控制的是 `_wait_for_final()` 的超时。V2.1 中上游（9001）收到 `speech_end_candidate` 后会直接通知 ASR `stop`，relay 收到 `stop` 后向 DashScope 提交结束标志并等待 final。由于 DashScope 实时 ASR 在收到 stop 信号后通常能在 200ms 内返回 final，因此 `ASR_FINAL_WAIT_MS=200` 即可。超时则 fallback 到最后一次 partial。
- `ASR_MAX_END_SILENCE_MS` 透传给 DashScope SDK 的 `max_sentence_silence` 参数。V2.1 中前端 VAD 已经在 250-350ms 静音后发送 `speech_end_candidate`，因此模型侧可以适当收紧到 300ms。

#### 4.2.3 改造影响模块

| 文件 | 改造内容 |
| --- | --- |
| `.env` / `deploy/env.prod` | 更新 `ASR_FINAL_WAIT_MS` 和 `ASR_MAX_END_SILENCE_MS` 的默认值 |
| `core/config.py` | 更新 `ASR_FINAL_WAIT_MS` 和 `ASR_MAX_END_SILENCE_MS` 的代码默认值 |

### 4.3 roundId 透传

#### 4.3.1 目标

支持上游在 `start` 消息中传入 `roundId`，relay 在所有响应消息中回传 `roundId`，便于端到端链路追踪和前端多轮音频隔离。

#### 4.3.2 Session 模型扩展

`models/session.py` 的 `Session` 类新增字段：

```python
self.round_id: str = ""
```

#### 4.3.3 ASR 链路 roundId 透传

| 步骤 | 改造 |
| --- | --- |
| `start` 消息 | 解析 `data.get("roundId", "")` 并存入 `session.round_id` |
| `resume` 消息 | 同上 |
| `partial` 响应 | 增加 `"roundId": session.round_id` |
| `final` 响应 | 增加 `"roundId": session.round_id` |
| 错误响应 | 增加 `"roundId": session.round_id` |

#### 4.3.4 TTS 链路 roundId 透传

| 步骤 | 改造 |
| --- | --- |
| `start` 消息 | 解析 `data.get("roundId", "")` 并存入 `session.round_id` |
| `tts_start` 响应 | 包含 `"roundId": session.round_id` |
| `tts_end` 响应 | 包含 `"roundId": session.round_id` |
| `completed` 响应 | 增加 `"roundId": session.round_id` |
| `cancelled` 响应 | 增加 `"roundId": session.round_id` |

#### 4.3.5 改造影响模块

| 文件 | 改造内容 |
| --- | --- |
| `models/session.py` | Session 类增加 `round_id` 字段 |
| `services/asr_realtime_service.py` | `handle_sentence()` 输出中增加 `roundId` |
| `routers/asr_ws.py` | `start`/`resume` 中解析 roundId |
| `services/tts_realtime_service.py` | 所有输出消息增加 `roundId` |
| `routers/tts_ws.py` | `start` 中解析 roundId |

### 4.4 ASR partial 补充 stable 字段

#### 4.4.1 目标

V2.1 协议中 `partial_asr` 需要包含 `stable` 字段。DashScope SDK 的 `sentence` 回调中包含 `stash_results` 字段，可用于判断 partial 稳定性。

#### 4.4.2 改造

在 `asr_realtime_service.py` 的 `handle_sentence()` 方法中，`partial` 消息增加：

```python
"stable": bool(sentence.get("stash_results", {}).get("fix", False))
```

#### 4.4.3 改造影响模块

| 文件 | 改造内容 |
| --- | --- |
| `services/asr_realtime_service.py` | `handle_sentence()` 的 partial 分支增加 `stable` 字段 |

### 4.5 降级链路 codec 补齐

#### 4.5.1 目标

降级模式下，TTS 同步 fallback 也需要在音频数据前后发送 `tts_start` / `tts_end`，确保上游播放行为一致。

#### 4.5.2 改造

`degrade_service.py` 的 `tts_sync_fallback()` 方法改造为：

1. 调用 `tts_service.synthesize()` 获取整段音频
2. 发送 `tts_start` JSON（含 codec、sampleRate、channels、`degraded: true`）
3. 分 chunk 发送二进制音频
4. 发送 `tts_end` JSON

#### 4.5.3 改造影响模块

| 文件 | 改造内容 |
| --- | --- |
| `services/degrade_service.py` | `tts_sync_fallback()` 增加 `tts_start` / `tts_end` 发送；接受 `round_id` 参数 |

### 4.6 TTS 输出格式强制约束

#### 4.6.1 目标

V2.1 明确要求主链路下行音频统一为 `pcm_s16le`。voice-relay 需要确保 TTS 输出格式为 PCM，避免 MP3 与前端 PCM 播放器不兼容的问题。

#### 4.6.2 当前状态

当前 `.env` 中 `TTS_AUDIO_FORMAT=pcm`，DashScope SDK 使用 `AudioFormat.PCM_16000HZ_MONO_16BIT`，已经满足要求。

#### 4.6.3 需要加固的点

1. `config.py` 中 `TTS_AUDIO_FORMAT` 默认值从 `mp3` 改为 `pcm`
2. 启动时增加校验：当 `TTS_AUDIO_FORMAT` 不是 `pcm` 或 `wav` 时打印 WARNING 日志，提示不建议在 V2.1 主链路中使用 MP3
3. `tts_start` 消息的 codec 映射要准确反映实际输出格式

#### 4.6.4 改造影响模块

| 文件 | 改造内容 |
| --- | --- |
| `core/config.py` | `TTS_AUDIO_FORMAT` 默认值改为 `pcm`；启动校验增加格式建议 |

## 5. 配置整改

### 5.1 需调整的配置项

| 配置项 | 当前默认值 | V2.1 目标值 | 说明 |
| --- | --- | --- | --- |
| `ASR_FINAL_WAIT_MS` | `1200` | `200` | 代码默认值调整 |
| `ASR_MAX_END_SILENCE_MS` | `600` | `300` | 代码默认值调整 |
| `TTS_AUDIO_FORMAT` | `mp3` | `pcm` | 代码默认值调整 |

### 5.2 .env 配置调整

```env
# V2.1 ASR 调优
ASR_FINAL_WAIT_MS=200
ASR_MAX_END_SILENCE_MS=300

# V2.1 TTS 格式统一
TTS_AUDIO_FORMAT=pcm
```

### 5.3 deploy/env.prod 配置调整

同上，生产环境配置文件同步更新。

## 6. 协议变更汇总

### 6.1 ASR `/asr/realtime` 协议变更

#### 客户端 → 服务端（新增字段）

`start` 消息新增可选字段：

```json
{
  "type": "start",
  "sessionId": "optional",
  "terminalId": "T001",
  "roundId": "r001"
}
```

#### 服务端 → 客户端（变更）

`partial` 消息新增字段：

```json
{
  "type": "partial",
  "text": "当前终端",
  "sessionId": "xxx",
  "traceId": "xxx",
  "roundId": "r001",
  "stable": false
}
```

`final` 消息新增字段：

```json
{
  "type": "final",
  "text": "当前终端有多少台",
  "sessionId": "xxx",
  "traceId": "xxx",
  "roundId": "r001",
  "fallbackFromPartial": false
}
```

### 6.2 TTS `/tts/realtime` 协议变更

#### 客户端 → 服务端（新增字段）

`start` 消息新增可选字段：

```json
{
  "type": "start",
  "sessionId": "optional",
  "terminalId": "T001",
  "roundId": "r001"
}
```

#### 服务端 → 客户端（新增消息）

新增 `tts_start`（首个音频 chunk 之前发送）：

```json
{
  "type": "tts_start",
  "sessionId": "xxx",
  "traceId": "xxx",
  "roundId": "r001",
  "codec": "pcm_s16le",
  "sampleRate": 16000,
  "channels": 1
}
```

新增 `tts_end`（所有音频 chunk 发送完毕后发送）：

```json
{
  "type": "tts_end",
  "sessionId": "xxx",
  "traceId": "xxx",
  "roundId": "r001",
  "reason": "completed"
}
```

`completed` 消息新增字段：

```json
{
  "type": "completed",
  "sessionId": "xxx",
  "traceId": "xxx",
  "roundId": "r001"
}
```

`cancelled` 消息新增字段：

```json
{
  "type": "cancelled",
  "sessionId": "xxx",
  "traceId": "xxx",
  "roundId": "r001"
}
```

### 6.3 向后兼容性

- `roundId` 为可选字段，上游不传时默认为空字符串，不影响现有链路
- `tts_start` / `tts_end` 为新增消息，上游若不处理可忽略
- `stable` 为新增字段，上游若不使用可忽略
- 现有 `partial` / `final` / `completed` / `cancelled` 消息类型名不变，保持向后兼容

## 7. 分阶段实施方案

### Phase 1：配置调优与格式加固

**目标：** 先通过参数调整降低延迟，加固 PCM 输出格式约束。

**改造内容：**
- `config.py` 中 `ASR_FINAL_WAIT_MS` 默认值改为 `200`
- `config.py` 中 `ASR_MAX_END_SILENCE_MS` 默认值改为 `300`
- `config.py` 中 `TTS_AUDIO_FORMAT` 默认值改为 `pcm`
- `.env` 和 `deploy/env.prod` 同步更新
- 启动校验增加 TTS 格式建议日志

**预期收益：**
- 首响延迟可立即缩短约 0.3-1s
- 消除 MP3 误用风险

### Phase 2：TTS 协议升级

**目标：** 补齐 `tts_start` / `tts_end` 元信息消息。

**改造内容：**
- `tts_realtime_service.py` 首包前发送 `tts_start`
- `tts_realtime_service.py` 完成后发送 `tts_end`
- `routers/tts_ws.py` 解析 `roundId`
- `degrade_service.py` 降级链路同步补齐 `tts_start` / `tts_end`
- `models/session.py` 增加 `round_id` 字段

**预期收益：**
- 上游可以正确获知音频格式，按 codec 初始化播放器
- 上游可以精确判断一轮 TTS 的开始与结束

### Phase 3：roundId 透传与 stable 补充

**目标：** 完善链路追踪能力和 ASR partial 稳定性标识。

**改造内容：**
- ASR 链路（`asr_ws.py` + `asr_realtime_service.py`）增加 roundId 解析与回传
- ASR partial 消息增加 `stable` 字段
- TTS 链路所有响应增加 roundId

**预期收益：**
- 端到端可按 roundId 追踪
- 上游可利用 stable 优化 partial 展示策略

## 8. 改造文件清单

| 文件 | Phase | 改造类型 | 改造内容摘要 |
| --- | --- | --- | --- |
| `app/core/config.py` | P1 | 修改 | ASR/TTS 默认参数调整 |
| `.env` | P1 | 修改 | ASR/TTS 参数值更新 |
| `deploy/env.prod` | P1 | 修改 | ASR/TTS 参数值更新 |
| `app/models/session.py` | P2 | 修改 | 增加 `round_id` 字段 |
| `app/services/tts_realtime_service.py` | P2 | 修改 | 增加 `tts_start`/`tts_end` 发送逻辑 |
| `app/routers/tts_ws.py` | P2 | 修改 | 解析 roundId；完善 finish/cancel 流程 |
| `app/services/degrade_service.py` | P2 | 修改 | 降级链路补齐 codec 元信息 |
| `app/services/asr_realtime_service.py` | P3 | 修改 | partial 增加 stable/roundId；final 增加 roundId |
| `app/routers/asr_ws.py` | P3 | 修改 | start/resume 解析 roundId |

## 9. 验收标准

### 9.1 功能验收

1. TTS 实时链路：首个音频 chunk 前收到 `tts_start`（含 codec/sampleRate/channels），所有 chunk 后收到 `tts_end`。
2. TTS 降级链路：同样收到 `tts_start` / `tts_end`，且 `degraded: true`。
3. TTS cancel 场景：`tts_end` 的 `reason` 为 `cancelled`。
4. ASR `stop` 后在 200ms 内返回 `final` 或 fallback。
5. ASR `partial` 消息包含 `stable` 字段。
6. 所有 ASR/TTS 响应消息包含 `roundId`（上游传入时）。
7. `TTS_AUDIO_FORMAT` 非 PCM/WAV 时启动日志打印 WARNING。
8. 向后兼容：上游不传 `roundId` 时，所有现有功能正常。

### 9.2 性能验收

| 指标 | 目标值 |
| --- | --- |
| ASR `stop` → `final` 返回（含 DashScope 处理） | ≤ 500ms |
| ASR `stop` → fallback 超时 | ≤ 200ms |
| TTS 首 chunk → `tts_start` + Binary 送出 | ≤ 10ms |
| `tts_start` 消息发送引入的额外延迟 | ≤ 5ms |

## 10. 风险与注意事项

### 10.1 ASR_FINAL_WAIT_MS 调低的风险

将 `ASR_FINAL_WAIT_MS` 从 15000ms 降至 200ms 后，如果 DashScope 模型在 200ms 内未返回 final，会 fallback 到最后一次 partial。这在大多数场景下是可接受的（partial 已经包含主要识别文本），但在极少数复杂句尾场景下可能导致 final 文本与 partial 不一致。

**缓解措施：** fallback 时已标记 `fallbackFromPartial: true`，上游可据此做差异化处理。

### 10.2 ASR_MAX_END_SILENCE_MS 调低的风险

从 600ms 降至 300ms 后，用户说话时如果有较长的自然停顿（如思考、犹豫），可能被误判为句尾。

**缓解措施：** V2.1 中上游已支持 `speech_resume`，即使误判句尾，用户继续说话时可恢复。此参数也支持上游在 `start` 消息中通过 `maxSentenceSilenceMs` 动态覆盖。

### 10.3 协议升级的过渡期

`tts_start` / `tts_end` 和 `roundId` 是新增内容，上游（9001 Java 后端）需要同步改造才能利用。过渡期内上游可以忽略这些新消息，不影响现有功能。

建议上下游协同发布顺序：**先升级 voice-relay（新增消息不影响旧上游），再升级 9001 Java 后端（开始消费新消息）**。
