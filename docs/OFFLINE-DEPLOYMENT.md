# Sudowork (Dify) 离线部署手册

适用场景：客户机房无外网，只有一台 Linux 服务器，需要一次性把整套
Sudowork-flavored Dify 跑起来。

---

## 1. 系统要求

| 项 | 最低 | 推荐 |
|---|---|---|
| OS | Ubuntu 22.04 / RHEL 9 / Debian 12 (x86\_64 / amd64) | 同 |
| CPU | 4 核 | 8 核 |
| 内存 | 8 GB | 16 GB |
| 磁盘 | 50 GB SSD | 200 GB SSD |
| Docker Engine | 24.0+ | 27.x |
| Docker Compose | v2.20+ | v2.27+ |
| 网络 | 内网即可，部署期间不需要公网 | 同 |

> **注意**：本手册假定 Docker Engine 和 Compose plugin 已在客户机器上
> 装好。如果客户机没装，需要先用本地 `.deb` / `.rpm` 包或厂商内网 yum
> 源装好，再回到第 2 步。

---

## 2. 离线包内容

GitHub Actions（或本地 `build-offline-bundle.sh`）每次只产出 **dify-api
和 dify-web 两份镜像**，不包含上游依赖镜像，包内文件结构如下：

```
sudowork-dify-offline-<version>-<short-sha>-amd64.zip
└── sudowork-dify-offline-<version>-<short-sha>-amd64/
    ├── docker/                  # 整个 docker/ 目录，含 sudowork-patches、env 模板等
    ├── images/                  # 仅含 fork 出的两个镜像
    │   ├── sudowork__dify-api__sudo-<version>-<sha>.tar
    │   ├── sudowork__dify-web__sudo-<version>-<sha>.tar
    │   └── manifest.txt         # 镜像清单（含 sha256）
    ├── install.sh               # 一键安装脚本
    └── README.md                # 简版部署清单（本文件简化版）
```

整包大小预估：**约 2-3 GB**（两份 fork 镜像）。仅 linux/amd64。

### 2.1 依赖镜像（由运维另行准备）

下列上游镜像 **不在** zip 包里，运维需要在客户机上提前 `docker load`
好（或者用客户机本地已经存在的版本）：

| 镜像 | 用途 |
|---|---|
| `langgenius/dify-plugin-daemon:0.6.1-local` | 插件守护进程 |
| `langgenius/dify-sandbox:0.2.15` | 代码沙箱 |
| `nginx:latest` | 反向代理 |
| `postgres:15-alpine` | 元数据库 |
| `redis:6-alpine` | 缓存 / 队列 |
| `semitechnologies/weaviate:1.27.0` | 向量库（默认） |
| `ubuntu/squid:latest` | SSRF proxy |

如果客户用别的向量库（qdrant / milvus / pgvector 等），换对应镜像；
对照 `docker/docker-compose.yaml` 顶部的 image 字段即可。

运维侧的标准操作：在能联网的工作机上 `docker pull` + `docker save -o
dep-images.tar <list>`，然后把 `dep-images.tar` 拷到客户机
`docker load -i dep-images.tar`。这样后续每次升级只需要换 fork 镜像
的小 zip 包，依赖镜像不用重传。

---

## 3. 上传到客户机

```bash
# 在你的工作机
scp sudowork-dify-offline-1.14.2-abc123def456-amd64.zip user@host:/opt/

# 在客户机
ssh user@host
cd /opt
unzip sudowork-dify-offline-1.14.2-abc123def456-amd64.zip
cd sudowork-dify-offline-1.14.2-abc123def456-amd64/
```

---

## 4. 安装

### 4.1 一键安装（推荐）

```bash
sudo ./install.sh
```

脚本会做这些事：

1. `docker load` 加载 `images/*.tar`（仅 dify-api、dify-web 两个）
2. 校验镜像 sha256 与 `manifest.txt` 一致
3. **检查依赖镜像**（plugin-daemon、sandbox、nginx、postgres、redis、
   weaviate、squid）是否已在本机；不在则报错退出。带
   `--skip-dep-check` 跳过此检查
4. 进入 `docker/`，如果没有 `.env` 就从 `.env.example` 复制
5. 为新部署生成随机密钥并写入 `.env`：
   - `SECRET_KEY`
   - `PLUGIN_DAEMON_KEY`
   - `SUDOWORK_SSO_SECRET`
   - `SUDOWORK_SYSTEM_SECRET`
   - `SUDOWORK_SYSTEM_TOKEN`
   - `DB_PASSWORD` / `REDIS_PASSWORD` / `WEAVIATE_API_KEY`
6. 把 `SUDO_DIFY_API_IMAGE` / `SUDO_DIFY_WEB_IMAGE` 设置为本次 zip 包
   实际包含的镜像 tag（带 commit SHA）
7. 提示填入 `CONSOLE_API_URL` / `APP_API_URL` 等公网域名（如果客户提供）
8. `docker compose up -d` 拉起所有服务
9. 等所有容器 healthy

### 4.2 手动安装（脚本失败时的兜底）

```bash
# 1. 先确认依赖镜像在本机（运维准备好）
docker images | grep -E "plugin-daemon|sandbox|nginx|postgres|redis|weaviate|squid"

# 2. 加载 fork 镜像
cd images/
for f in *.tar; do
  echo "loading $f"
  docker load -i "$f"
done

# 3. 准备 env
cd ../docker/
cp .env.example .env
# 编辑 .env，填入：
#   - SUDO_DIFY_API_IMAGE / SUDO_DIFY_WEB_IMAGE（写本次 zip 包里的镜像 tag）
#   - SECRET_KEY=<openssl rand -base64 42>
#   - PLUGIN_DAEMON_KEY=<openssl rand -base64 48>
#   - SUDOWORK_SSO_SECRET / SUDOWORK_SYSTEM_SECRET / SUDOWORK_SYSTEM_TOKEN（必须）
#   - CONSOLE_API_URL / APP_API_URL（客户访问域名）
vim .env

# 4. 启动
docker compose up -d

# 5. 看状态
docker compose ps
```

---

## 5. 验证

```bash
# 全部容器应该 Up + healthy（少数无 healthcheck 的只显示 Up）
docker compose ps

# 浏览器访问（默认端口 6808，避免和客户机已有的 80 冲突）
curl -I http://localhost:6808/
# 期望：301 redirect 到 /apps，或者 200

# 健康检查
curl -s http://localhost:6808/console/api/system-features | head -c 200

# 看日志（出错时）
docker compose logs --tail 100 api
docker compose logs --tail 100 web
docker compose logs --tail 100 nginx
```

---

## 6. 生成离线包

### 6.1 推荐：GitHub Actions

`build-push.yml` workflow 在以下场景自动出包：

- push 到 `main` / `release/**` / `build/**`，或
- 推 `v*` 形式的 tag，或
- 在 GitHub 上手动触发 `workflow_dispatch`（可填 `version` 输入覆盖默认
  `1.14.2`）

产物 `sudowork-dify-offline-<ver>-<short-sha>-amd64.zip` 作为
workflow artifact 上传，保留 30 天。**不会** push 任何镜像到 ghcr.io
或其它仓库。

### 6.2 本地手工出包

前置：本机已经 build 出 `sudowork/dify-api:sudo-<ver>-<sha>` 和
`sudowork/dify-web:sudo-<ver>-<sha>`（或使用 `--prebuilt-images-dir`
指向已经 docker save 好的 tar 目录）。

```bash
cd dify/docker/
./build-offline-bundle.sh
# 输出：sudowork-dify-offline-<version>-<short-sha>-amd64.zip
```

脚本会：

1. `docker save` 本机的 `sudowork/dify-api` 和 `sudowork/dify-web`
2. 复制 `docker/` 目录（自动剔除 `volumes/db|redis|weaviate` 等
   runtime 数据，但保留 `plugin_packages/`）
3. 生成 `install.sh` + `manifest.txt`
4. 打包成 `.zip`

详细参数：

```bash
./build-offline-bundle.sh --version 1.14.2
./build-offline-bundle.sh --short-sha abc123def456   # 默认从 git 读
./build-offline-bundle.sh --prebuilt-images-dir /tmp/prebuilt
./build-offline-bundle.sh --help
```

---

## 7. 升级

```bash
# 工作机：去 GitHub Actions 下载新版 zip（artifact），或本地重出
git pull
cd docker && ./build-offline-bundle.sh
scp sudowork-dify-offline-<new-ver>-<new-sha>-amd64.zip user@host:/opt/

# 客户机
cd /opt
unzip sudowork-dify-offline-<new-ver>-<new-sha>-amd64.zip
cd sudowork-dify-offline-<new-ver>-<new-sha>-amd64/

# 保留旧 .env，把它复制到新目录
cp /opt/sudowork-dify-offline-<old-ver>-<old-sha>-amd64/docker/.env docker/.env
sudo ./install.sh --upgrade   # --upgrade 跳过密钥生成；image tag 还是会被重新 pin
```

---

## 8. 回滚

升级时旧版的 `sudowork-dify-offline-<old-ver>-<old-sha>-amd64/` 目录
仍然在 `/opt/` 下没动过（只是新版被解压到了新目录）。回滚直接切回旧
目录起来即可，旧版镜像还在本机 docker 缓存里：

```bash
docker compose -f /opt/sudowork-dify-offline-<new-ver>-<new-sha>-amd64/docker/docker-compose.yaml down
cd /opt/sudowork-dify-offline-<old-ver>-<old-sha>-amd64/
docker compose up -d
```

数据库不动（pg / redis volume 保留），所以业务数据没事。

---

## 9. 常见问题

**Q：客户机已经装了别的 Docker，能直接 `docker compose up`？**  
A：可以，但要确认现有 `docker network` 没冲突。所有服务都在
`docker_default` 网络下，namespace 不会撞。

**Q：第二次部署 `install.sh` 会清掉数据吗？**  
A：不会。`docker compose down` 默认保留 volume。只有显式
`docker compose down -v` 才会清。

**Q：模型供应商插件是否需要联网安装？**  
A：不需要。`docker/volumes/plugin_daemon/plugin_packages/langgenius/`
已经预置 45 个主流 provider 的 `.difypkg`（OpenAI、Anthropic、Gemini、
通义、智谱、DeepSeek、Ollama…）。Tenant provision 时自动从本地缓存
安装。详见 `docs/plans/2026-06-17-dify-integration-design.md` §
P3.5b。

**Q：怎么改 Dify Studio 的工作空间名？**  
A：在 sudowork-server 管理端改企业名即可——会通过 Inner API 同步
到 Dify tenant。

**Q：能多机部署吗？**  
A：本手册只覆盖单机。多机部署需要：把 postgres / redis / weaviate
拆出去用独立服务，改 `.env` 里 `DB_HOST` / `REDIS_HOST` 指向外部
endpoint。

---

## 10. 相关文档

- `docs/plans/2026-06-17-dify-integration-design.md` —— 整体集成设计
- `docs/plans/2026-06-23-dify-rebrand-to-sudowork.md` —— 品牌替换细节
- `docker/sudowork-patches/` —— 所有 patch 文件
- `docker/.env.example` —— 全部环境变量说明
