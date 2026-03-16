# voice-relay 镜像打包与离线部署

## 1. 结论
你的理解是对的。

正确流程应该是：

1. 在能上网的跳板机上，用当前仓库里的 `Dockerfile` 正常构建镜像
2. 在跳板机上验证镜像能跑
3. 把最终镜像 `docker save` 成 tar
4. 把镜像 tar、`docker-compose.yml`、`.env` 上传到内网服务器
5. 内网服务器执行 `docker load` 后直接运行镜像

内网服务器不需要：

- `Dockerfile`
- 项目源码
- `requirements.txt`
- 单独下载 Python 依赖
- 单独下载 `ffmpeg`

内网服务器只需要已经安装好的 Docker / Docker Compose，以及你打好的最终镜像。

## 2. 跳板机构建镜像
以下命令默认在跳板机的 Linux shell 执行。

### 2.1 进入项目目录
```bash
cd /path/to/voice-relay
```

### 2.2 构建最终镜像
```bash
docker build -t voice-relay:latest .
```

### 2.3 本地验证镜像
```bash
docker run --rm -d --name voice-relay-test --env-file .env -p 9000:9000 voice-relay:latest
sleep 5
curl http://127.0.0.1:9000/health
docker rm -f voice-relay-test
```

期望返回：

```json
{"status":"ok"}
```

## 3. 跳板机打离线包
### 3.1 准备打包目录
```bash
mkdir -p offline-package
```

### 3.2 收集启动文件
```bash
cp docker-compose.yml offline-package/
cp .env offline-package/
```

如果你不想把真实密钥跟镜像一起传输，就不要复制 `.env`，改为到服务器手工创建 `.env`。

### 3.3 导出最终镜像
```bash
docker save -o offline-package/voice-relay_latest.tar voice-relay:latest
```

### 3.4 压缩离线包
```bash
tar -czf voice-relay_offline_bundle.tar.gz offline-package
ls -lh voice-relay_offline_bundle.tar.gz
```

### 3.5 上传到内网服务器
```bash
scp voice-relay_offline_bundle.tar.gz user@your-server:/opt/
```

## 4. 内网服务器导入并启动
### 4.1 解压离线包
```bash
cd /opt
tar -xzf voice-relay_offline_bundle.tar.gz
cd offline-package
ls -lh
```

### 4.2 导入镜像
```bash
docker load -i voice-relay_latest.tar
docker image ls | grep voice-relay
```

### 4.3 启动服务
现在的 `docker-compose.yml` 已经是纯镜像运行模式，不会触发构建：

```bash
docker compose up -d
docker compose ps
```

如果你的环境还是旧版 Compose：

```bash
docker-compose up -d
docker-compose ps
```

### 4.4 验证服务
```bash
curl http://127.0.0.1:9000/health
docker compose logs --tail=100
```

如果你在 `.env` 里把 `PORT` 改成了别的值，验证时把上面的 `9000` 改成对应端口。

## 5. 以后更新版本
每次代码更新都重复下面这套动作即可：

### 5.1 跳板机重新构建和导出
```bash
cd /path/to/voice-relay
docker build -t voice-relay:latest .
docker save -o offline-package/voice-relay_latest.tar voice-relay:latest
tar -czf voice-relay_offline_bundle.tar.gz offline-package
scp voice-relay_offline_bundle.tar.gz user@your-server:/opt/
```

### 5.2 服务器重新导入和启动
```bash
cd /opt
tar -xzf voice-relay_offline_bundle.tar.gz
cd /opt/offline-package
docker load -i voice-relay_latest.tar
docker compose up -d
```

## 6. 补充说明
- 跳板机负责联网下载依赖和安装 `ffmpeg`
- 内网服务器只负责加载和运行最终镜像
- 内网服务器不需要 `Dockerfile`
- 内网服务器也不需要项目源码
- 如果你后面想把这个流程做成一键脚本，我可以继续给你补 `build_and_pack.sh` 和 `deploy_from_bundle.sh`
