# Voice Relay 部署指导

## 脚本位置

部署脚本在：

[deploy_pipeline.py](/F:/Codework/hospital-asset-ai-voice/deploy_pipeline.py)

项目目录在：

[voice-relay](/F:/Codework/hospital-asset-ai-voice/voice-relay)

## 依赖

先安装 Python 依赖：

```bash
python -m pip install paramiko
```

## 流程一：服务器 A 构建镜像并下载到本机

执行：

```bash
python /F:/Codework/hospital-asset-ai-voice/deploy_pipeline.py --ahost 1.2.3.4 --apwd 123 --adir /home/test
```

如果服务器 A 用户不是 `root`，可以加：

```bash
--auser youruser
```

这个流程会做这些事：

1. 在本机把 `voice-relay` 的构建所需文件打成 `voice-relay-deploy.zip`
2. 上传到服务器 A 的 `--adir` 目录
3. 本机删除刚生成的 `voice-relay-deploy.zip`
4. 在服务器 A 解压并删除远程 zip
5. 删除旧容器 `voice-relay`
6. 删除旧镜像 `voice-relay:latest`
7. 在服务器 A 执行：

```bash
docker build -t voice-relay:latest .
docker save -o voice-relay_latest.tar voice-relay:latest
```

8. 把 `voice-relay_latest.tar` 下载到本机
9. 删除服务器 A 上的 `voice-relay_latest.tar`
10. 删除服务器 A 上解压后的 `voice-relay` 文件夹

本机下载镜像 tar 时，不需要额外再传别的密码参数。  
因为下载本身就是复用你已经提供的 `--apwd` 这个 SSH 登录密码。

## 流程二：上传到服务器 B 并启动容器

前提：流程一已经执行成功，并且本机目录下已有：

```bash
voice-relay_latest.tar
```

执行：

```bash
python /F:/Codework/hospital-asset-ai-voice/deploy_pipeline.py --bhost 1.2.3.4 --bpwd 123 --bdir /home/test
```

如果服务器 B 用户不是 `root`，可以加：

```bash
--buser youruser
```

这个流程会做这些事：

1. 检查本机是否存在 `voice-relay_latest.tar`
2. 将下面这些运行文件打成 `voice-relay.zip`
   - `voice-relay/.env`
   - `voice-relay/keywords.txt`
   - `voice-relay/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/` 下运行必需文件
3. 上传 `voice-relay_latest.tar` 到服务器 B 的 `--bdir`
4. 上传 `voice-relay.zip` 到服务器 B 的 `--bdir`
5. 本机删除刚上传完的 `voice-relay_latest.tar`
6. 本机删除刚上传完的 `voice-relay.zip`
7. 如果服务器 B 上已经有 `voice-relay` 文件夹，则不解压 `voice-relay.zip`，只删除 zip
8. 如果服务器 B 上没有 `voice-relay` 文件夹，则解压 `voice-relay.zip` 后删除 zip
9. 删除旧容器 `voice-relay`
10. 删除旧镜像 `voice-relay:latest`
11. 执行：

```bash
docker network inspect data-aiops-net >/dev/null 2>&1 || docker network create data-aiops-net
docker load -i /home/test/voice-relay_latest.tar
docker run -d \
  --name voice-relay \
  --network data-aiops-net \
  --restart unless-stopped \
  --env-file /home/test/voice-relay/.env \
  -p 9000:9000 \
  -v /home/test/voice-relay/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01:/app/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01:ro \
  -v /home/test/voice-relay/keywords.txt:/app/keywords.txt:ro \
  voice-relay:latest
```

## 一条命令全流程

如果你想直接 A 到 B 一把跑完，也支持：

```bash
python /F:/Codework/hospital-asset-ai-voice/deploy_pipeline.py --ahost 1.2.3.4 --apwd 123 --adir /home/test --bhost 5.6.7.8 --bpwd 456 --bdir /home/test
```

## 密码说明

这里的密码是：

```bash
SSH 登录密码
```

不是开机密码，不是系统启动密码。

也就是你平时执行下面这种命令时输入的那个密码：

```bash
ssh root@服务器IP
```

## 默认本地文件来源

脚本默认使用这些本地文件：

- [voice-relay/.env](/F:/Codework/hospital-asset-ai-voice/voice-relay/.env)
- [voice-relay/keywords.txt](/F:/Codework/hospital-asset-ai-voice/voice-relay/keywords.txt)
- [voice-relay/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01](/F:/Codework/hospital-asset-ai-voice/voice-relay/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01)

如果你要改路径，可以加参数：

```bash
--env xxx
--keywords xxx
--modeldir xxx
--workdir xxx
```
