# voice-relay 在服务器 B（麒麟 Linux）部署手册

## 1. 先说结论：这个项目到底需要什么

根据当前代码，这个项目是一个 `FastAPI + Uvicorn` 的 Python 服务，功能是把医院内网服务器 A 发来的请求转发到阿里云模型接口。

当前项目实际依赖如下：

- Python `3.10+`，建议直接用 `3.12`
- `pip`、`venv`
- Linux `systemd`
- `ffmpeg`
- 能访问公网的 DNS 和 HTTPS 出口
- 阿里云 DashScope 的 API Key

为什么必须装 `ffmpeg`：

- `app/services/asr_service.py` 里写死了：如果上传的音频不是 `wav/mp3/pcm`，就会调用 `ffmpeg` 转成 `wav`
- 项目目录里还放了一个 `m4a` 音频样例，这说明实际业务大概率会碰到 `m4a`
- 所以服务器 B 上如果没装 `ffmpeg`，ASR 很容易直接报错

当前服务默认监听端口：

- `9000`

当前接口：

- `GET /health` 
- `POST /asr/recognize`
- `POST /v1/chat/completions`
- `POST /tts/synthesize`

## 2. 你的网络拓扑应该满足什么

你的实际链路是：

1. 医院内网服务器 A 调用服务器 B 的 `voice-relay`
2. 服务器 B 再调用阿里云 DashScope

所以服务器 B 至少要满足：

- 入站：允许服务器 A 访问服务器 B 的 `9000/TCP`
- 出站：允许服务器 B 访问公网 `443/TCP`
- DNS：服务器 B 能解析 `dashscope.aliyuncs.com`

建议安全策略：

- 防火墙或安全组只放行服务器 A 的 IP 到 `9000`
- `.env` 里的 `ALLOWED_IPS` 也只写服务器 A 的 IP，不要继续保留整段私网白名单

## 3. 部署前准备清单

正式开工前，把下面这些信息准备好：

- 服务器 B 的登录方式：`root` 或可 `sudo` 的账号
- 服务器 B 的固定 IP
- 服务器 A 的内网 IP
- 阿里云 `DashScope API Key`
- 项目代码目录：`voice-relay`
- 服务器 B 的系统包管理器类型

先在服务器 B 上执行这几个命令，确认环境：

```bash
cat /etc/os-release
which dnf
which yum
which apt
python3 --version
```

说明：

- 麒麟 Linux 可能是 `dnf/yum` 系，也可能是 `apt` 系
- 不要先假设，先查

## 4. 安装系统环境

### 4.1 如果是 `apt` 系

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg curl tar
```

### 4.2 如果是 `dnf` 或 `yum` 系

先尝试：

```bash
sudo dnf install -y python3 python3-pip ffmpeg curl tar
```

如果你的系统没有 `dnf`，就改成：

```bash
sudo yum install -y python3 python3-pip ffmpeg curl tar
```

如果执行 `python3 -m venv venv` 时报缺少 venv 相关组件，再补装：

```bash
sudo dnf install -y python3-virtualenv
```

或：

```bash
sudo yum install -y python3-virtualenv
```

安装完成后验证：

```bash
python3 --version
pip3 --version
ffmpeg -version
```

建议目标：

- Python 版本不低于 `3.10`
- `ffmpeg` 命令可以正常输出版本号

## 5. 把项目代码传到服务器 B

推荐放在：

```bash
/opt/voice-relay
```

如果你是从 Windows 往 Linux 传，最省事的方法是用 `WinSCP` 或 `MobaXterm`，把本地目录：

```text
F:\Codework\hospital-asset-ai-voice\voice-relay
```

整体上传到服务器 B 的：

```text
/opt/voice-relay
```

上传后在服务器 B 上确认目录：

```bash
cd /opt/voice-relay
ls
```

你应该至少能看到这些文件：

- `app/`
- `requirements.txt`
- `.env.example`
- `systemd/voice-relay.service`
- `deploy.sh`

## 6. 创建运行账号

建议不要长期用 root 跑服务，单独建一个系统账号：

```bash
sudo useradd -r -s /sbin/nologin voice-relay
```

如果系统提示 `/sbin/nologin` 不存在，就改成：

```bash
sudo useradd -r -s /usr/sbin/nologin voice-relay
```

然后授权目录：

```bash
sudo chown -R voice-relay:voice-relay /opt/voice-relay
```

## 7. 配置项目环境变量

先复制模板：

```bash
cd /opt/voice-relay
cp .env.example .env
```

然后编辑：

```bash
vi .env
```

推荐你改成下面这种最小可用配置：

```env
CLOUD_API_KEY=这里填写你自己的阿里云DashScope密钥
CLOUD_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

ASR_MODEL=paraformer-realtime-v2
ASR_TIMEOUT=15

LLM_API_KEY=
LLM_API_URL=
LLM_MODEL=qwen-plus
LLM_TIMEOUT=30

TTS_MODEL=cosyvoice-v3-flash
TTS_VOICE=longanyang
TTS_TIMEOUT=15

PORT=9000
ALLOWED_IPS=服务器A的内网IP/32,127.0.0.1
LOG_LEVEL=INFO
```

举例：

```env
ALLOWED_IPS=10.20.30.40/32,127.0.0.1
```

如果服务器 A 可能从多个地址过来，就写多个，用英文逗号分隔：

```env
ALLOWED_IPS=10.20.30.40/32,10.20.30.41/32,127.0.0.1
```

注意：

- 当前仓库里的 `.env.example` 看起来像放了一个真实格式的 key，正式部署时不要直接照抄
- 如果这个 key 曾经真实使用过，建议你立即去阿里云控制台轮换

## 8. 创建 Python 虚拟环境并安装依赖

切到项目目录：

```bash
cd /opt/voice-relay
```

如果你刚才创建了 `voice-relay` 用户，建议用该用户安装：

```bash
sudo -u voice-relay python3 -m venv /opt/voice-relay/venv
sudo -u voice-relay /opt/voice-relay/venv/bin/pip install --upgrade pip setuptools wheel
sudo -u voice-relay /opt/voice-relay/venv/bin/pip install -r /opt/voice-relay/requirements.txt
```

如果你暂时先不用专门账号，也可以直接安装：

```bash
python3 -m venv /opt/voice-relay/venv
/opt/voice-relay/venv/bin/pip install --upgrade pip setuptools wheel
/opt/voice-relay/venv/bin/pip install -r /opt/voice-relay/requirements.txt
```

## 9. 先手工启动一次，验证服务能不能跑

这一步非常重要，不要一上来就配 systemd。

在项目目录执行：

```bash
cd /opt/voice-relay
/opt/voice-relay/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 9000
```

看到类似下面日志，说明启动成功：

```text
Application startup complete
Uvicorn running on http://0.0.0.0:9000
```

另开一个终端检查健康接口：

```bash
curl http://127.0.0.1:9000/health
```

期望返回：

```json
{"status":"ok"}
```

如果这里都不通，就先不要继续配 systemd。

## 10. 再做接口联调验证

### 10.1 LLM 接口测试

```bash
curl -X POST "http://127.0.0.1:9000/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model":"qwen-plus",
    "messages":[
      {"role":"user","content":"你好，请回复测试成功"}
    ]
  }'
```

### 10.2 TTS 接口测试

```bash
curl -X POST "http://127.0.0.1:9000/tts/synthesize" \
  -H "Content-Type: application/json" \
  -d '{"text":"你好，这是语音合成测试","speed":1.0}' \
  --output test.mp3
```

如果当前目录生成了 `test.mp3`，说明 TTS 通了。

### 10.3 ASR 接口测试

服务器上准备一个测试音频，比如 `test.m4a`，然后执行：

```bash
curl -X POST "http://127.0.0.1:9000/asr/recognize" \
  -F "audio=@test.m4a"
```

如果你传的是 `m4a`，这里能不能成功，取决于 `ffmpeg` 是否正确安装。

## 11. 配置 systemd 开机自启

虽然仓库里已经有 `systemd/voice-relay.service`，但当前版本有一个运维层面的坑：

- 它把 `ExecStart` 里的监听端口写死成了 `9000`
- 如果你以后改 `.env` 里的 `PORT`，服务实际监听端口和配置文件可能不一致

所以部署时建议你直接新建一个更稳的 systemd 文件。

执行：

```bash
sudo vi /etc/systemd/system/voice-relay.service
```

写入下面内容：

```ini
[Unit]
Description=Voice Relay Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=voice-relay
Group=voice-relay
WorkingDirectory=/opt/voice-relay
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/opt/voice-relay/.env
ExecStart=/opt/voice-relay/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

然后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable voice-relay
sudo systemctl start voice-relay
sudo systemctl status voice-relay --no-pager
```

## 12. 配置防火墙

原则很简单：

- 只允许服务器 A 访问服务器 B 的 `9000`
- 不要对全网开放

如果你的系统使用 `firewalld`，可参考：

```bash
sudo firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=10.20.30.40/32 port protocol=tcp port=9000 accept'
sudo firewall-cmd --reload
```

更稳妥的做法：

- 安全组放行服务器 A 到 `9000`
- 操作系统防火墙也只放行服务器 A 到 `9000`

如果你们医院网络已经有边界防火墙统一控流量，也至少要确认 `9000` 不是对公网完全开放。

## 13. 上线后怎么检查

### 13.1 看服务状态

```bash
systemctl status voice-relay --no-pager
```

### 13.2 看实时日志

```bash
journalctl -u voice-relay -f
```

### 13.3 看端口监听

```bash
ss -lntp | grep 9000
```

### 13.4 看服务器 B 能不能访问阿里云

```bash
curl -I https://dashscope.aliyuncs.com
```

如果这里都不通，LLM/TTS/ASR 一定会报错。

## 14. 以后怎么更新版本

以后你改了代码，标准更新流程如下：

1. 把新代码上传覆盖到 `/opt/voice-relay`
2. 保留原来的 `.env` 不要被覆盖
3. 重新安装依赖
4. 重启服务
5. 重新测 `/health`

命令如下：

```bash
cd /opt/voice-relay
/opt/voice-relay/venv/bin/pip install -r requirements.txt
sudo systemctl restart voice-relay
curl http://127.0.0.1:9000/health
```

## 15. 最常见故障和处理办法

### 15.1 报 `ffmpeg: No such file or directory`

原因：

- 没安装 `ffmpeg`

处理：

```bash
ffmpeg -version
```

如果命令不存在，先安装 `ffmpeg`。

### 15.2 返回 `403 forbidden`

原因：

- `.env` 中 `ALLOWED_IPS` 没有包含服务器 A 的真实来源 IP

处理：

- 先在日志里看请求来源 IP
- 把该 IP 加到 `ALLOWED_IPS`
- 改完后重启服务

### 15.3 返回 `502` 或 `504`

原因通常是：

- 阿里云 Key 不对
- 服务器 B 访问不了公网
- DNS 解析不到 `dashscope.aliyuncs.com`
- 上游超时

处理顺序：

1. `curl -I https://dashscope.aliyuncs.com`
2. 检查 `.env` 里的 `CLOUD_API_KEY`
3. 看 `journalctl -u voice-relay -f`

### 15.4 `/health` 通，但业务接口不通

原因：

- 应用本身启动了，但访问阿里云的外网链路没通

处理：

- 先测服务器 B 出网
- 再测 API Key

### 15.5 改了 `.env` 的 `PORT`，但服务还是监听 9000

原因：

- 你用了仓库里原始的 `systemd/voice-relay.service`
- 那个文件把端口写死了

处理：

- 按本手册第 11 节的 systemd 文件改

## 16. 你这套项目最终上线时，建议这样定

为了简单稳定，推荐你最后按下面这套定稿：

- 部署目录：`/opt/voice-relay`
- 运行用户：`voice-relay`
- Python：`3.12`
- 服务端口：`9000`
- 访问来源：只允许服务器 A 的 IP
- 进程管理：`systemd`
- 日志查看：`journalctl`
- 外网依赖：`dashscope.aliyuncs.com:443`

---

如果你不想自己手敲，我建议实际落地时就按这 5 步执行：

1. 安装 `python3 + pip + venv + ffmpeg`
2. 上传代码到 `/opt/voice-relay`
3. 配 `.env`
4. 先手工启动验证 `/health`
5. 再配 `systemd` 开机自启
