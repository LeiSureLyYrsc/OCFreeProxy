# OCFreeProxy

OpenCode Free 模型反代（OpenAI 兼容）—— FastAPI + Web UI + 代理池 + 冷却 + Docker / GHCR。

Python **3.12** · 依赖管理 **uv** · 镜像 `ghcr.io/leisurelyyrsc/ocfreeproxy:latest`

## 功能

- 自动拉取 OpenCode Zen 免费模型（`id` 以 `-free` 结尾，或 input/output 价格为 0，如 `big-pickle`）
- 本地 OpenAI 兼容接口：`GET /v1/models`、`POST /v1/chat/completions`（含流式）
- **Clash 订阅 URL** 导入；**粘贴导入**（订阅 URL / Clash YAML / Base64 节点列表 / 逐行 URI）——输入栏粘贴，不读系统剪贴板
- 代理池轮换（LRU）；上游 **429 / FreeUsageLimit** 时按 `Retry-After` 冷却，否则冷却至下一 **UTC 午夜**
- **「使用本地连接」开关**：开启后 `direct`（本机出口）进入代理池，也可被冷却
- Web 管理页：模型、代理状态、订阅、设置
- SQLite 持久化（Docker 卷 `/data`）
- `docker-compose.yml` + GitHub Actions 构建推送 GHCR

## 代理协议说明（重要）

v1 **实际用于请求出口**的协议：

| 类型 | 是否可用于调用 |
|------|----------------|
| HTTP / HTTPS 代理 | ✅ |
| SOCKS5 | ✅ |
| Clash YAML 中的 `type: http` / `socks5` | ✅ 自动提取 |
| vmess / vless / trojan / ss / hysteria 等 | ⚠️ 可导入并列出，标记为 `unsupported`，**不会**用于 httpx 出口 |

如需完整 Clash 协议栈，请在宿主机或旁路运行 Clash Meta / mihomo，把其 **mixed-port / socks-port** 以 `socks5://127.0.0.1:port` 手动加入池中。

## 快速开始（Docker Compose）

```bash
git clone https://github.com/LeiSureLyYrsc/OCFreeProxy.git
cd OCFreeProxy
mkdir -p data
docker compose up -d
```

浏览器打开 http://localhost:8080

镜像地址（Compose 已写好）：

```yaml
image: ghcr.io/leisurelyyrsc/ocfreeproxy:latest
```

## 本地开发（uv）

```bash
# 需要 Python 3.12+
uv sync
export DATA_DIR=./data
uv run uvicorn ocfreeproxy.main:app --host 0.0.0.0 --port 8080
```

或：

```bash
uv run ocfreeproxy
```

## 环境变量

| 变量 | 说明 | 默认 |
|------|------|------|
| `ACCESS_TOKEN` | 网关鉴权（Bearer / x-api-key）；空则不鉴权 | 空 |
| `UPSTREAM_BASE_URL` | 上游 Zen Base | `https://opencode.ai/zen/v1` |
| `OPENCODE_API_KEY` | 上游 API Key（如需要） | 空 |
| `DATA_DIR` | 数据目录 | `./data`（容器内 `/data`） |
| `USE_DIRECT` | 初始是否把本地连接加入代理池 | `true` |
| `HOST` / `PORT` | 监听 | `0.0.0.0` / `8080` |

## API 示例

```bash
# 模型列表
curl http://127.0.0.1:8080/v1/models

# 对话（非流式）
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"beam-free","messages":[{"role":"user","content":"hi"}]}'

# 若设置了 ACCESS_TOKEN
curl http://127.0.0.1:8080/v1/models -H "Authorization: Bearer YOUR_TOKEN"
```

响应头 `X-OCFreeProxy-Egress` / `X-OCFreeProxy-Egress-Name` 标识本次使用的出口。

## Web UI

- **使用本地连接**：开关开启 → `direct` 参与轮换；关闭 → 仅配置的代理（无可用代理时请求返回 503）
- Clash URL / 粘贴订阅 / 手动 HTTP·SOCKS5
- 代理状态：`active` / `cooling` / `disabled` / `unsupported` / `excluded`

## 冷却策略

OpenCode Free 额度按 **出口 IP**（IPv6 常见按 `/64`）限制，按 **UTC 自然日**重置。收到 429 或 FreeUsageLimit 时：

1. 优先使用响应头 `Retry-After`（秒或 HTTP-date）
2. 否则冷却至下一个 UTC 午夜
3. 该出口在冷却期内不会被选中；可自动切换池内其它出口重试

## 项目结构

```
src/ocfreeproxy/
  main.py            # FastAPI 入口
  config.py          # 环境配置
  store.py           # SQLite 持久化
  subscription.py    # Clash / Base64 / URI 解析
  proxy_pool.py      # 代理池 + 本地开关
  cooldown.py        # Retry-After / UTC 午夜
  models_client.py   # 上游模型与补全转发
  routes_api.py      # /v1 + /api
  routes_web.py      # 页面
  templates/ static/
Dockerfile
docker-compose.yml
.github/workflows/docker.yml
```

## License

MIT
