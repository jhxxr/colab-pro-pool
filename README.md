# Colab Pro Pool — 多 Pro 账号云端算力池调度系统（Web 控制台 + AI Agent）

面向课题组与自动化研发工作流的多 Google Colab Pro / Pro+ 账号池中心调度服务端。将多个付费 Pro 账号集中托管在一台 Linux 服务器上，提供 **一体化 Web 控制台**、**标准 MCP 工具协议**、**HTTP REST API** 与 **CLI 客户端**。

## 核心特性

1. **多 Pro 账号 `HOME` 强隔离与并发池锁**：基于官方开源 [`google-colab-cli`](https://github.com/googlecolab/google-colab-cli) 构建，按 `/var/lib/colab-pool/accounts/<name>` 独立隔离每个账号的 OAuth 凭据与 `sessions.json`，结合 SQLite WAL 原子事务实现“一账号一并发槽位”租约锁、优先级队列与 LRU 均衡轮询。
2. **配额耗尽自动容灾切换（Failover）**：当某个 Pro 账号算力单元（CU）耗尽或目标卡型无容量返回 `FAILED_PRECONDITION` / `RESOURCE_EXHAUSTED` 时，自动将该账号置入冷却期并无缝切换下一个空闲 Pro 账号继续拉起实例（遇到 OAuth 失效时标记 `[AUTH_REQUIRED]` 并延长冷却）。同时支持套餐识别（`T4`/`L4`/`A100`/TPU 分配至普通 Pro，`H100`/`G4` 自动路由至 `pro+` 槽位）。
3. **一体化 Web 控制面板（单端口 `8080` 托管）**：
   - **算力总览与槽位矩阵**：实时查看空闲/忙碌/冷却倒计时、成功率和 GPU 分布，支持网页启停账号、修改套餐、解除冷却、扫描同步与一键清理孤儿实例。
   - **网页添加与授权账号**：点击“添加账号”生成 Google 授权链接，在浏览器完成授权后粘贴授权码，即可自动保存凭据并加入账号池；支持重新授权、取消、超时释放与刷新页面后恢复授权会话。
   - **CU 余额与每小时消耗**：总览和账号页直接显示各账号剩余 CU、当前消耗速率（CU/h）、运行实例数与更新时间；支持每分钟自动更新和手动刷新。
   - **运行型号与配置价格目录**：账号卡片和任务详情展示实测 GPU、显存、vCPU、系统内存和磁盘；“型号与价格”列出 CPU、GPU 和 TPU 参考配置，保存本池各配置的实测 CU/h，并可按自填的每 CU 成本在小字中换算货币费用。
   - **SSE 实时终端日志 & 产物浏览器**：支持关键词过滤、日志下载，以及训练图像（PNG/JPG/SVG）和指标文件（JSON/CSV/TXT）网页直接预览。
   - **提交实验工作台**：支持在线脚本编辑器（含多文件添加和模板）或拖拽上传 `.py`/`.ipynb`/`.tar.gz`，支持优先级、环境变量与克隆配置重跑。
4. **公开数据集机房内高速直连 + 产物自动回收**：支持 `dataset_commands` 钩子让 Colab 利用 Google 机房带宽直接拉取公开数据集（`wget` / `curl` / `hf download` / `gdown`）；训练写入 `/content/outputs`（环境变量 `$COLAB_OUTPUT_DIR`）的模型权重与日志会在 `finally` 销毁实例前自动打包拉回中心服务器，并自动探测记录远端 `nvidia-smi` 实测 GPU 型号。

---

## 快速部署（Linux 服务器）

### 1. Docker Compose 使用发布镜像（推荐）

复制 `.env.example` 为 `.env`，将 `COLAB_POOL_API_KEY` 改成随机长密钥，然后启动：

```bash
cp .env.example .env
# 编辑 .env，设置自己的 COLAB_POOL_API_KEY
docker compose pull
docker compose up -d --no-build
```

镜像为 `ghcr.io/jhxxr/colab-pro-pool:latest`，同时提供 `linux/amd64` 与 `linux/arm64`。生产环境可在 `.env` 中通过 `COLAB_POOL_IMAGE=ghcr.io/jhxxr/colab-pro-pool:v0.1.0` 固定版本。更新时重新执行上面的 `pull` 和 `up` 命令。

需要自行构建时执行 `docker compose up -d --build`。`Dockerfile` 采用多阶段构建，自动编译 `frontend/` 并打入 FastAPI 静态目录，内置健康检查与固定版本的官方 `google-colab-cli`。

> 默认仅在服务端绑定 `127.0.0.1:8080`，避免明文 HTTP 暴露公网。如需允许局域网或 Tailscale 网段访问，可设置 `COLAB_POOL_BIND_ADDR=0.0.0.0` 并配合防火墙控制来源。

### 2. 原生安装（Linux / macOS，Python 3.12+）

```bash
pip install google-colab-cli
apt-get update && apt-get install -y rclone
# 从源码安装时，先编译静态产物：
cd frontend && npm ci && npm run build && cd ..
pip install -e .
```

### 3. 添加并授权多个 Colab Pro 账号槽位

启动下方服务后，进入 Web 控制台“账号池与云盘档案”：

1. 点击“添加账号”，输入槽位名称（例如 `pro_01`）并选择套餐等级。
2. 点击“生成 Google 授权链接”，再点击“打开 Google 授权”。
3. 登录该槽位对应的 Google 账号，勾选全部请求的权限；将 Google 最终页面显示的授权码粘贴回控制台。
4. 点击“完成授权并加入账号池”，验证成功后账号自动可用于调度。已有账号可点击“网页授权”或“重新授权”。

网页流程沿用官方 CLI 的远程授权码机制，无需在服务器终端输入，也无需额外配置回调域名；仍需将 Google 显示的授权码粘贴回网页。账号授权默认限时 10 分钟，关闭弹窗或按 Esc 会取消授权；刷新页面后可恢复当前会话。服务重启后需重新生成链接。

授权过程使用临时隔离目录，新账号成功后才注册；已有账号重新授权时进入 `maintenance`，与任务调度和孤儿清理互斥。失败或取消会保留旧凭据与启停状态。OAuth 凭据通过项目提供的 CLI keyring 后端独立写入各账号的 `.config/colab-cli/pool-oauth.json`（明文文件，POSIX 权限 `0600`），不依赖宿主机共享钥匙串。已有账号目录中的旧 `token.json` 会由 CLI 迁移到新存储；曾仅写入系统钥匙串的账号需要逐个重新授权。

也可在服务器终端依次初始化账号：

```bash
./scripts/init_pro_account.sh pro_01 pro
./scripts/init_pro_account.sh pro_02 pro
./scripts/init_pro_account.sh pro_plus_01 pro+
```

> **防坑提示**：把终端打印的 Google 授权链接复制到本地浏览器打开时，**必须勾选页面上的全部 4 个权限复选框**，漏勾任一项都会触发每小时强制掉线重登（[google-colab-cli Issue #116](https://github.com/googlecolab/google-colab-cli/issues/116)）。

授权 API（沿用控制台 API Key）：`POST /api/v1/account-auth` 发起，`GET /api/v1/account-auth/{session_id}` 查询，`POST /api/v1/account-auth/{session_id}/code` 提交 `{"code":"..."}`，`DELETE /api/v1/account-auth/{session_id}` 取消。授权码和 token 不返回给前端，也不写入服务日志。可用 `COLAB_POOL_AUTH_TIMEOUT_SECONDS` 调整会话时限，用 `COLAB_POOL_AUTH_STARTUP_TIMEOUT_SECONDS` 调整 CLI 生成链接的等待时间（默认 30 秒）。

账号列表中的 `usage` 包含 `remaining_cu`、`consumption_rate_hourly`（CU/h）、`active_assignments`、`updated_at`、`checked_at`、`refreshing`、`stale` 与 `error`。数据来自官方 `colab --auth oauth2 usage`，需要支持 `usage` 的新版 `google-colab-cli`。消耗速率为该 Google 账号所有运行实例的合计，包含控制台之外开启的机器；查询本身不会分配或停止运行实例。

服务启动后后台查询并缓存用量，默认每 60 秒更新，最多同时查询两个账号；页面轮询无需等待 CLI。新机器完成硬件探测后会提前刷新空闲时的旧费率，无需等满一分钟。已知的零余额或零消耗显示 `0.00`，未授权、尚未查到或查询失败显示 `—`；已有成功结果时保留旧数值并标出错误和上次更新时间。重新授权成功后会清除原账号余额，防止同一槽位换绑 Google 账号时显示旧数据。缓存保存在 SQLite，重启后仍可查看。

`POST /api/v1/accounts/{name}/usage/refresh` 刷新单个账号，`POST /api/v1/pool/usage/refresh` 刷新全部账号；均沿用 API Key 并立即返回 `202`，随后通过账号列表查看结果。重复查询会合并，手动刷新最短间隔 5 秒。可设置 `COLAB_POOL_USAGE_CACHE_SECONDS`（默认 60，最短 5 秒）和 `COLAB_POOL_USAGE_TIMEOUT_SECONDS`（默认 25）调整缓存与单次查询超时。

“型号与价格”通过 `GET /api/v1/resources` 提供参考显存 / HBM、推荐账号等级、最近实测配置和 CU/h。型号列表是可请求的目标，不代表当前一定有容量。机器启动后采集硬件；请求型号与实际 GPU 分开显示，不会仅根据用户选择就声称已分配到该 GPU。

仅在账号恰好有一个活动实例、任务正在运行且 CU 数据晚于硬件探测时，才把该账号的 CU/h 存为单台机器的历史参考价格；多实例账号不参与单台价格采样。不同内存和硬件配置分别保留记录，任务结束、删除或服务重启后仍可比较；尚无记录的型号显示“待实测”。TPU HBM 为每芯片参考规格，实际拓扑仍以分配结果为准。可选的每 CU 成本和币种仅保存在当前浏览器，主价格始终为 CU/h，货币估算以小字显示。

目前可自动确认并归档 GPU 和 CPU 的单实例费率；TPU 显示请求型号和参考 HBM，尚未自动确认芯片型号 / 拓扑，因此不把账号消耗冒充为已确认的 TPU 型号报价。

### 4. 启动调度服务端

```bash
export COLAB_POOL_DATA_DIR=/var/lib/colab-pool
export COLAB_POOL_API_KEY="your-strong-secret-key"
export COLAB_POOL_CORS_ORIGINS="*"
colab-pool-server
```

服务启动时会自动扫描所有账号并执行一次孤儿实例清理（`colab stop`），防止异常重启残留机器空烧 CU。

- 手动清理时，账号会临时进入 `maintenance`（清理维护中）状态，与任务分配互斥；运行中的账号始终跳过。
- 通过 API 删除账号且保留磁盘目录（默认 `remove_disk=false`）时，会写入 `.removed` 标记，后续扫描和服务重启不会自动重新注册；显式注册同名账号可恢复。`remove_disk=true` 则同时移除账号目录。

---

## 本地如何连接服务器上的服务

Web 控制台、HTTP REST API、SSE 日志流与 MCP 接口全部托管在同一个 `8080` 端口。推荐按以下三种方式之一打通连接：

### 方式一：SSH 端口转发（最推荐，单人零公网暴露）

1. 服务器保持默认 `127.0.0.1:8080` 绑定。
2. 在本地终端运行：
   ```bash
   ssh -N -L 8080:127.0.0.1:8080 user@your-server-ip
   ```
3. 本地浏览器访问 **`http://127.0.0.1:8080`**，输入 `COLAB_POOL_API_KEY` 解锁控制台；MCP 与 CLI 的 `COLAB_POOL_SERVER_URL` 填 `http://127.0.0.1:8080`。

### 方式二：Tailscale / 内网组网（课题组多人共用）

1. 服务器与个人电脑均接入 Tailscale（服务器获得固定内网 IP 如 `100.88.x.x`）。
2. 服务器设置 `COLAB_POOL_BIND_ADDR=0.0.0.0` 并通过防火墙仅对 `tailscale0` 网卡放行 `8080`。
3. 组内成员浏览器、MCP、CLI 直接连接 `http://100.88.x.x:8080`。

### 方式三：域名 + Caddy HTTPS 反向代理（公网访问）

使用 Caddy 自动签发 TLS 证书，并关闭响应缓冲以保证 SSE 实时日志流畅：

```caddyfile
colab-pool.yourdomain.com {
    reverse_proxy 127.0.0.1:8080 {
        flush_interval -1
    }
}
```

---

## AI Agent 与 CLI 接入

### MCP Server（Claude Code / Hermes Agent / Cursor / Codex）

```json
{
  "mcpServers": {
    "colab-pro-pool": {
      "command": "colab-pool-mcp",
      "env": {
        "COLAB_POOL_SERVER_URL": "http://127.0.0.1:8080",
        "COLAB_POOL_API_KEY": "your-strong-secret-key"
      }
    }
  }
}
```

内置 7 个 MCP 工具：`colab_pool_status`、`submit_colab_job`、`list_colab_jobs`、`get_colab_job`、`retry_colab_job`、`download_colab_artifacts`、`cancel_colab_job`。

### 主力 CLI 客户端（`cpool` / `colab-pool`）

安装本包后即可直接使用短命令 `cpool`（或别名 `colab-pool`），支持持久化登录档案（`~/.config/colab-pool/config.json`）、`.cpoolignore` 打包忽略、表格展示与 `--json` 脚本模式、SSE 实时日志流及账号池运维：

```bash
# 1. 建立 SSH 隧道（或在另一个终端窗口启动）
cpool tunnel user@your-linux-server-ip

# 2. 登录并持久化服务端地址与 API Key（支持多环境 --profile）
cpool login http://127.0.0.1:8080 --api-key "your-strong-secret-key"

# 3. 查看算力池状态与账号矩阵
cpool status

# 4. 提交本地脚本或项目目录（自动忽略 .git / venv / .cpoolignore），走 SSE 实时看日志并拉取产物
cpool run ./my_experiment -e train.py --gpu A100 --priority 10 --pip einops --wait -o ./downloaded_outputs

# 5. 一行代码快速上卡执行
cpool exec -c "import torch; print(torch.cuda.get_device_name(0))" --gpu T4

# 6. 追踪任意任务、单独拉取某个输出文件或管理账号池
cpool logs <job_id> -f
cpool wait <job_id> -o ./downloaded_outputs
cpool pull <job_id> ./res --file metrics.json
cpool accounts cleanup
```

（原有 `colab-pool-client` 入口继续保留兼容。）

## 前端本地开发与自动化测试

```bash
# 前端热更新开发（默认代理 /api 与 /healthz 到 127.0.0.1:8080）
cd frontend
npm install
npm run dev

# 编译生产静态产物至 colab_pool/static/
npm run build

# 运行全量后端与接口自动化测试
cd ..
python -m pytest -v
```

## GitHub Actions 自动发版

`.github/workflows/ci.yml` 在推送 `main` 或提交 Pull Request 时运行 Python 3.10 / 3.12 测试、前端生产构建、Python wheel / sdist 构建和安装检查。

发布时先同步修改以下版本号并提交：

- `pyproject.toml` 的 `project.version`
- `colab_pool/__init__.py` 的 `__version__`
- `frontend/package.json` 的 `version`（在 `frontend/` 执行 `npm version 0.2.0 --no-git-tag-version` 会同时更新 `package-lock.json`）

随后推送对应的稳定版本标签：

```bash
git tag -a v0.2.0 -m "Release v0.2.0"
git push origin main
git push origin v0.2.0
```

也可在 GitHub Actions 的 **Release → Run workflow** 中从 `main` 手动发布当前项目版本，成功后会自动创建对应标签。版本格式为 `MAJOR.MINOR.PATCH`，标签必须为 `vMAJOR.MINOR.PATCH`；版本不一致或标签已指向其他提交时会拒绝发布。

发布流程重新运行全部 CI，在原生 amd64 / arm64 runner 上构建并启动容器，检查官方 Colab CLI、Web 页面、健康端点和 API 鉴权，再推送 GHCR 镜像并合并双架构清单。镜像提供 `v0.2.0`、`0.2.0` 等版本标签；不低于现有 Latest 的版本还会更新 `latest`，补发旧版本不会覆盖最新版本。

镜像发布成功后自动创建正式 GitHub Release，上传包含生产前端的 `.whl` / `.tar.gz`、镜像清单、拉取说明及 `SHA256SUMS`。流程使用仓库自带的 `GITHUB_TOKEN`，无需额外配置 Docker Hub 密码或 PAT；工作流已声明所需的 `contents: write` 和 `packages: write` 权限。第三方 Actions 固定到提交 SHA，由 Dependabot 每周检查更新。

GHCR 首次发布后，请在对应 package 的 **Package settings → Change visibility** 确认其为 **Public**，以便未登录 Docker 的用户拉取。仓库公开与 package 公开是独立设置。
