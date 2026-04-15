# voice-relay 三套 TTS 接口并存改动方案

## 1. 目标确认

按当前决策，TTS 不做“替换式切换”，而做“并存式扩展”。

最终保留三套彼此独立的接口：

1. 同步 TTS：继续保留现有 `cosyvoice-v3-flash`
2. 实时 TTS：继续保留现有 `cosyvoice-v3-flash`
3. 新增实时 TTS：单独接入 `qwen3-tts-flash-realtime`

这样后续可以直接做对比压测、首包时延对比、音质对比、稳定性对比，等验证完成后，再删除慢或效果差的那一套实时接口。

## 2. 当前接口现状

当前仓库里已有两套 TTS 接口：

- 同步接口：`POST /tts/synthesize`
  - 文件：`voice-relay/app/routers/tts.py`
  - 实现：`voice-relay/app/services/tts_service.py`
  - 下游 SDK：`dashscope.audio.tts_v2.SpeechSynthesizer`
- 实时接口：`WS /tts/realtime`
  - 文件：`voice-relay/app/routers/tts_ws.py`
  - 实现：`voice-relay/app/services/tts_realtime_service.py`
  - 下游 SDK：`dashscope.audio.tts_v2.SpeechSynthesizer`

也就是说，目前同步和实时虽然是两个接口，但底层都是 CosyVoice 这套 `tts_v2` 接法。

## 3. 本次改造原则

这次不改现有两套接口的行为，不把 `qwen3-tts-flash-realtime` 塞进现有 `/tts/realtime`。

建议原则：

- 现有同步 CosyVoice 不动
- 现有实时 CosyVoice 不动
- 新增一个独立的 Qwen Realtime 路由、服务类、配置项、测试脚本
- 三套接口互不影响
- 统一复用公共会话管理、鉴权、日志、限流能力

这样做的好处是：

- 风险小，现网链路不被破坏
- 回滚简单，只要不调用新接口即可
- 对比更清晰，同一终端可以切不同实时接口验证
- 最后淘汰旧链路时，只需要删对应 router/service/config，不会牵连另一个实时实现

## 4. 建议的最终接口布局

建议保留和新增如下：

| 类型 | 路径 | 模型 | 状态 |
| --- | --- | --- | --- |
| 同步 TTS | `POST /tts/synthesize` | `cosyvoice-v3-flash` | 保留 |
| 实时 TTS（旧） | `WS /tts/realtime` | `cosyvoice-v3-flash` | 保留 |
| 实时 TTS（新） | `WS /tts/realtime/qwen` | `qwen3-tts-flash-realtime` | 新增 |

这里推荐新增路由使用：

- `WS /tts/realtime/qwen`

原因：

- 和现有 `/tts/realtime` 语义接近
- 调用方很容易理解这是“Qwen 版实时 TTS”
- 后面如果还要扩第三套，也方便继续按 `/tts/realtime/{provider}` 扩展

## 5. 是否需要改动

需要改，而且主要是“新增接入”，不是“替换旧接入”。

原因如下：

- `qwen3-tts-flash-realtime` 不是当前 `dashscope.audio.tts_v2.SpeechSynthesizer` 这套接口
- 它官方使用的是 `dashscope.audio.qwen_tts_realtime.QwenTtsRealtime`
- 生命周期也不同：
  - 先 `connect()`
  - 再 `session.update`
  - 文本用 `append_text`
  - 结束用 `finish`
  - 音频通过 `response.audio.delta` 事件返回
- 当前 CosyVoice 实现依赖的是 `on_data(bytes)`、`streaming_call()`、`async_streaming_complete()`、`streaming_cancel()`

所以：

- 现有 `tts_realtime_service.py` 不能直接只改模型名复用成 Qwen
- 更合适的做法是新增一个独立的 Qwen Realtime Service

## 6. 推荐实现结构

### 6.1 保留现有文件不动

这些文件尽量不改或只做极小改动：

- `voice-relay/app/services/tts_service.py`
- `voice-relay/app/services/tts_realtime_service.py`
- `voice-relay/app/routers/tts.py`
- `voice-relay/app/routers/tts_ws.py`

目标是：

- 让现有同步 CosyVoice 和实时 CosyVoice 继续按原逻辑跑

### 6.2 新增 Qwen Realtime 专用文件

建议新增：

- `voice-relay/app/services/tts_qwen_realtime_service.py`
- `voice-relay/app/routers/tts_qwen_ws.py`

职责建议：

- `tts_qwen_realtime_service.py`
  - 只负责 `qwen3-tts-flash-realtime`
  - 封装 `QwenTtsRealtime` SDK
  - 解析 `response.audio.delta`
  - 向上游继续输出已有的 `tts_start`、二进制音频、`tts_end`
- `tts_qwen_ws.py`
  - 提供独立的 WebSocket 入口
  - 入口协议尽量与现有 `/tts/realtime` 保持一致

这样终端接入层可以做到：

- 消息格式不变
- 只切连接地址就能比较两个实时接口

## 7. 新接口与旧接口的协议关系

建议对上游终端保持同一套消息协议，不要让终端因为换模型而重写交互逻辑。

建议 `WS /tts/realtime/qwen` 继续沿用现有实时接口协议：

客户端发：

- `start`
- `text`
- `finish`
- `cancel`
- `ping`

服务端回：

- `tts_start`
- 二进制音频
- `tts_end`
- `completed`
- `error`
- `pong`

这样终端侧只需要切换 WebSocket URL：

- 旧实时：`/tts/realtime`
- 新实时：`/tts/realtime/qwen`

其余逻辑完全不变，便于直接横向对比。

## 8. Qwen Realtime 下游映射方案

`/tts/realtime/qwen` 建议采用如下映射：

| 上游事件 | Qwen SDK / 协议 |
| --- | --- |
| `start` | `connect()` + `session.update(...)` |
| `text` | `append_text(content)` |
| `finish` | `finish()` |
| `cancel` | `cancel_response()` 或直接 `close()` |
| `response.audio.delta` | Base64 解码后 `send_bytes()` |
| 首个音频包到达 | 回上游 `tts_start` |
| `session.finished` | 回上游 `tts_end` + `completed` |
| `error` | 回上游 `error` |

注意点：

- Qwen Realtime 音频不是直接 bytes 回调，而是 JSON 事件中的 Base64 字段
- 完成态应以 `session.finished` 或最终完成事件为准
- 不能复用 CosyVoice 的 `on_complete()` 逻辑

## 9. 配置方案

### 9.1 现有 CosyVoice 配置保留

当前配置保留给原两套接口使用：

- `TTS_SYNC_MODEL`
- `TTS_REALTIME_MODEL`
- `TTS_VOICE`
- `TTS_AUDIO_FORMAT`

如果后续想更清晰，也可以第二阶段再重命名为更明确的 `COSY_*` 前缀，但这不是本次必须项。

### 9.2 新增 Qwen Realtime 专用配置

建议新增以下配置项：

- `QWEN_TTS_REALTIME_MODEL=qwen3-tts-flash-realtime`
- `QWEN_TTS_REALTIME_VOICE=<待选音色>`
- `QWEN_TTS_REALTIME_WS_URL=ws://.../api-ws/v1/realtime`
- `QWEN_TTS_REALTIME_AUDIO_FORMAT=pcm`
- `QWEN_TTS_REALTIME_SAMPLE_RATE=16000`
- `QWEN_TTS_REALTIME_MODE=server_commit`
- `QWEN_TTS_REALTIME_TIMEOUT=15`
- `QWEN_TTS_REALTIME_FIRST_CHUNK_TIMEOUT=3`

建议原因：

- 配置彻底与 CosyVoice 解耦
- 两个实时接口后续可以独立调参
- 做对比时不用反复改同一批环境变量

## 10. 采样率建议

建议新 Qwen 实时接口第一版先固定：

- `pcm`
- `16000Hz`

原因：

- 当前系统现有实时接口上游通告给终端的是 `16000`
- 先保持播放链路一致，更利于公平比较“首包速度、稳定性、音质主观感受”
- 避免 Qwen 默认 24k 与现有播放器处理方式不一致，造成“声音变速/失真”的伪问题

如果后续验证终端完全支持 24k，再单独开启第二轮优化。

## 11. 会话与限流建议

虽然三个接口逻辑独立，但建议仍复用现有：

- `SessionService`
- IP 白名单
- 公共日志
- 公共 metrics

建议策略：

- 三个 TTS 接口共享总的 TTS 并发配额
- 但在日志和 metrics 中区分 provider

例如增加维度：

- `provider=cosy_sync`
- `provider=cosy_realtime`
- `provider=qwen_realtime`

这样可以更直观看出：

- 哪个接口报错更多
- 哪个接口首包更慢
- 哪个接口吞吐更差

## 12. 降级策略建议

这次不要把 Qwen Realtime 强行接入现有 CosyVoice 降级逻辑里。

建议：

- 现有 `/tts/realtime` 继续保留当前降级逻辑
- 新增 `/tts/realtime/qwen` 可以先做成“失败即报错，不自动切到 cosy”

原因：

- 你现在的目标是对比两套实时接口
- 如果 Qwen 实时失败后悄悄回退到 CosyVoice，会把对比数据污染掉
- 很难判断到底是哪条链路在工作

后续如果需要生产兜底，再给 Qwen 实时接口增加可开关的 fallback。

## 13. 需要修改/新增的文件

### 13.1 必改

- `voice-relay/app/main.py`
  - 挂载新增 `tts_qwen_ws.router`
- `voice-relay/app/core/config.py`
  - 增加 `QWEN_TTS_REALTIME_*` 配置
- `voice-relay/deploy/env.prod`
  - 增加新接口配置
- `voice-relay/DEPLOY.md`
  - 增加第三套接口说明

### 13.2 必增

- `voice-relay/app/services/tts_qwen_realtime_service.py`
- `voice-relay/app/routers/tts_qwen_ws.py`

### 13.3 建议补充

- `测试/test_tts_realtime.py`
  - 明确标注它是 CosyVoice 实时测试
- 新增 `测试/test_tts_qwen_realtime.py`
  - 专门测试新接口

## 14. 建议的实施步骤

### 第一阶段：新增接口，不动旧接口

1. 新建 `tts_qwen_realtime_service.py`
2. 新建 `tts_qwen_ws.py`
3. 在 `main.py` 挂载新路由
4. 新增 `QWEN_TTS_REALTIME_*` 配置

产出结果：

- 旧同步接口可用
- 旧实时接口可用
- 新实时 Qwen 接口可用

### 第二阶段：补对比能力

1. 给日志加 provider 字段
2. 给 metrics 区分 provider
3. 分别跑首包耗时、整段耗时、取消响应测试

### 第三阶段：做淘汰决策

对比项建议：

1. 首包时延
2. 全段完成时延
3. 取消响应速度
4. 音质主观评价
5. 失败率
6. 长文本稳定性

验证完成后，只删除表现慢或不稳定的那一个实时接口即可。

## 15. 风险点

### 15.1 跳板机地址风险

当前配置已有 `/api-ws/v1/inference`，但 Qwen Realtime 需要 `/api-ws/v1/realtime`。

如果跳板机没有把 realtime 路由代理出去，新接口会直接连不上。

### 15.2 配置混用风险

如果继续复用旧变量名，容易把：

- CosyVoice 实时模型
- Qwen 实时模型
- 同步模型

配错到一起。

因此建议 Qwen 新接口单独使用 `QWEN_TTS_REALTIME_*` 前缀。

### 15.3 对比数据污染风险

如果 Qwen 实时失败后自动降级成 CosyVoice，同一条请求看上去“成功了”，但实际不是 Qwen 结果，会影响对比结论。

## 16. 最小可行方案

如果这次只做最小必要改动，建议范围如下：

1. 保持 `POST /tts/synthesize` 不动
2. 保持 `WS /tts/realtime` 不动
3. 新增 `WS /tts/realtime/qwen`
4. 新增 `tts_qwen_realtime_service.py`
5. 新增 `QWEN_TTS_REALTIME_*` 配置
6. 新增一个 Qwen 实时测试脚本

这是最符合当前目标的方案：低风险、便于对比、后续删除也干净。

## 17. 验收标准

满足以下条件，视为本次改造完成：

1. 现有同步 CosyVoice 接口完全不受影响
2. 现有实时 CosyVoice 接口完全不受影响
3. 新增 Qwen 实时接口可独立连通
4. 三个接口配置彼此独立
5. 终端只需切 URL 即可比较两个实时接口
6. 至少具备一套对比测试脚本

## 18. 结论

按你现在的目标，最合适的实现方式不是“把 `/tts/realtime` 替换成 Qwen”，而是：

- 保留现有同步 CosyVoice
- 保留现有实时 CosyVoice
- 额外新增一个独立的 Qwen Realtime 接口

这是当前最稳、最适合做性能和效果对比的方案。

