# voice-relay Docker 部署文档

## 前置条件

- 本地已安装 Docker Desktop（Windows）
- 服务器已安装 Docker
- 服务器能访问公网（用于 `docker build` 拉取依赖）

---

## 第一步：本地打 zip 包

在 **本地 PowerShell** 中执行（路径根据实际调整）：

```powershell
$src = "f:\Codework\hospital-asset-ai-voice\voice-relay"
$tmp = "f:\Codework\hospital-asset-ai-voice\voice-relay-deploy"
$modelSrc = "$src\sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
$modelDst = "$tmp\models\sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"

if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
New-Item -ItemType Directory -Path $tmp | Out-Null
New-Item -ItemType Directory -Path "$tmp\models" | Out-Null

Copy-Item "$src\Dockerfile" $tmp
Copy-Item "$src\requirements.txt" $tmp
Copy-Item "$src\docker-compose.yml" $tmp
Copy-Item "$src\keywords.txt" $tmp
Copy-Item "$src\.env.example" $tmp
Copy-Item "$src\app" "$tmp\app" -Recurse

New-Item -ItemType Directory -Path $modelDst | Out-Null
Copy-Item "$modelSrc\encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx" $modelDst
Copy-Item "$modelSrc\decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx" $modelDst
Copy-Item "$modelSrc\joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx" $modelDst
Copy-Item "$modelSrc\tokens.txt" $modelDst

$zipPath = "f:\Codework\hospital-asset-ai-voice\voice-relay-deploy.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path "$tmp\*" -DestinationPath $zipPath
Remove-Item $tmp -Recurse -Force

Write-Host "打包完成: $zipPath"
```

zip 包内容结构：

```
voice-relay-deploy.zip
├── Dockerfile
├── requirements.txt
├── docker-compose.yml
├── keywords.txt
├── .env.example
├── app/
└── models/
    └── sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/
        ├── encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx
        ├── decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx
        ├── joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx
        └── tokens.txt
```

---

## 第二步：上传到服务器

```bash
scp f:\Codework\hospital-asset-ai-voice\voice-relay-deploy.zip root@your-server:/opt/
```

---

## 第三步：服务器解压

```bash
cd /opt
unzip voice-relay-deploy.zip -d voice-relay
cd voice-relay
ls -lh
```

---

## 第四步：配置 .env

```bash
cp .env.example .env
vim .env
```

必填项：

| 变量 | 说明 |
|------|------|
| `CLOUD_API_KEY` | 阿里云 DashScope API Key |
| `ASR_REALTIME_MODEL` | 实时 ASR 模型，如 `paraformer-realtime-v2` |
| `ASR_SYNC_MODEL` | 同步 ASR 模型，如 `paraformer-realtime-v2` |
| `LLM_DEFAULT_MODEL` | LLM 模型，如 `qwen-plus` |
| `TTS_REALTIME_MODEL` | 实时 TTS 模型，如 `cosyvoice-v3-flash` |
| `TTS_SYNC_MODEL` | 同步 TTS 模型，如 `cosyvoice-v3-flash` |
| `TTS_VOICE` | TTS 音色，如 `longanyang` |
| `WAKEUP_MODEL_DIR` | 填 `/app/models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01` |
| `WAKEUP_KEYWORDS_FILE` | 填 `/app/keywords.txt` |

---

## 第五步：构建镜像

```bash
docker build -t voice-relay:latest .
```

> 首次构建需要下载依赖，耗时约 3~5 分钟，取决于服务器网速。

---

## 第六步：验证镜像

启动测试容器：

```bash
docker run -d \
  --name voice-relay-test \
  --env-file .env \
  -p 9000:9000 \
  -v $(pwd)/models:/app/models:ro \
  -v $(pwd)/keywords.txt:/app/keywords.txt:ro \
  voice-relay:latest
```

等待约 5 秒后验证：

```bash
curl http://127.0.0.1:9000/health
```

期望返回：

```json
{"status":"ok"}
```

验证完毕，清理测试容器：

```bash
docker rm -f voice-relay-test
```

---

## 第七步：启动生产容器

> 服务器 Docker 版本较旧，使用 `docker run` 代替 `docker compose`。

```bash
docker run -d \
  --name voice-relay \
  --restart unless-stopped \
  --env-file .env \
  -p 9000:9000 \
  -v $(pwd)/models:/app/models:ro \
  -v $(pwd)/keywords.txt:/app/keywords.txt:ro \
  voice-relay:latest
```

查看运行状态：

```bash
docker ps | grep voice-relay
docker logs voice-relay --tail=50
```

---

## 第八步：保存镜像（备用）

将镜像导出为 tar 文件，便于后续在其他服务器直接导入使用，无需重新构建：

```bash
docker save -o voice-relay_latest.tar voice-relay:latest
ls -lh voice-relay_latest.tar
```

下次在其他服务器导入：

```bash
docker load -i voice-relay_latest.tar
```

---

## 常用运维命令

```bash
# 查看日志
docker logs voice-relay --tail=100 -f

# 重启容器
docker restart voice-relay

# 停止容器
docker stop voice-relay

# 删除容器
docker rm -f voice-relay

# 查看容器资源占用
docker stats voice-relay
```

---

## 更新版本

1. 本地修改代码后，重新打 zip 包（第一步）
2. 上传到服务器，重新解压覆盖（第二步、第三步）
3. 重新构建镜像：
   ```bash
   docker build -t voice-relay:latest .
   ```
4. 重启容器：
   ```bash
   docker rm -f voice-relay
   docker run -d \
     --name voice-relay \
     --restart unless-stopped \
     --env-file .env \
     -p 9000:9000 \
     -v $(pwd)/models:/app/models:ro \
     -v $(pwd)/keywords.txt:/app/keywords.txt:ro \
     voice-relay:latest
   ```
