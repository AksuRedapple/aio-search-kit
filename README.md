# aio-search-kit · 给 coding agent 的共享搜索底座

> 一个本地服务，把「搜索」变成**带缓存、带配额、带降级、带出处、可对账**的一层。
> 任何 harness（Claude Code / Codex / opencode / 你自己的 agent）都通过 MCP 或 HTTP 共用它。

## 它解决什么

每个 harness 自带一套搜索的后果：缓存不共享、出处不一致、成本没人算。
这个服务把三件事收拢到一处：

| 能力 | 一句话 |
|---|---|
| **缓存** | 同一条查询第二次是 0 成本（按字符二元组做近似命中，容忍改写差异） |
| **配额** | 每个调用方（harness）一个本地 token，按**次数**和**钱**双闸记账 |
| **降级** | 主 provider 超限或失败 → 自动切到下一个（默认 keyless Firecrawl），降级原因写进记录 |
| **对账** | `/stats` 回答四问：花了多少 / 命中率 / 降级分布 / 各 provider 的 p50-p95 |

## 快速开始（零第三方 Python 依赖）

```bash
# 1) 放配置（默认只用 keyless Firecrawl，不需要 API key）
mkdir -p ~/.aio-searchd
cp config.example.json ~/.aio-searchd/config.json

# 2) 起服务
python3 aio_searchd.py            # 127.0.0.1:8792

# 3) 试一下
curl -s "http://127.0.0.1:8792/search?q=redis+acl+whoami&live=1&harness=me" | python3 -m json.tool
curl -s "http://127.0.0.1:8792/stats" | python3 -m json.tool
```

## 接进 harness（MCP）

```jsonc
// Claude Code: ~/.claude/settings.json
{
  "mcpServers": {
    "aio-search": {
      "command": "python3",
      "args": ["/绝对路径/aio-search-kit/mcp_server.py"]
    }
  }
}
```

工具只有一个：`search(query, top?, live?)`。
**默认不花钱**（先查缓存）；显式 `live=true` 才做实时检索。

## 端点

| 端点 | 说明 |
|---|---|
| `GET /search?q=&top=8&live=0&harness=&origin=` | 命中返回 `cached:true`；未命中且 `live=0` 返回空 + 提示 |
| `POST /record` | `{query, sources:[{title,url,snippet}]}` → 写回共用缓存（**只收带 url 的**） |
| `GET /stats?days=7&origin=` | 四问表；`origin` 用来剔除测试流量 |
| `GET /journal` | 原始记录（JSONL） |
| `GET /health` | 就绪度 |

## 设计取舍（都是踩出来的）

1. **降级要有名字**：`quota-exceeded` / `firecrawl-fail` / `no-provider`，不是一个 `degraded:true`。
2. **缓存键用查询文本 + 二元组近似**：短查询 vs 改写长查询也能命中；纯 sha1 精确匹配在长尾负载下命中率≈0。
3. **没有出处的条目不入库**：`POST /record` 直接丢弃无 `url` 的结果。
4. **测试流量要能被剔除**：`origin=test` 与真实流量分开统计，否则命中率会被压测刷高。
5. **keyless 是备路不是主路**：免费额度按 IP 限流，随时可能收紧。

## 边界

- **只管搜索**，不抓正文、不做答案生成；
- **不做鉴权**：只绑 `127.0.0.1`，是本机服务，不是公网服务；
- 配额是**按调用方本地记账**，不是可信计量——它防的是"我忘了控制"，不是防攻击。

## License

MIT
