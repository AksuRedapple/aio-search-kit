# 跨 harness 搜索底座 · 设计（v1 草案）

> 问题：搜索能力**只长在 dsh 里**，Claude Code / Codex / opencode 各有一套，缓存、出处、成本互不相通。
> 目标：**一个底座，四个 harness 共用**——但不动它们的宿主框架。

## 一、为什么"共用面"是 MCP，不是插件

| harness | 能否装私有插件 | 能否接 MCP |
|---|---|---|
| dsh | ✅（cordis bundle） | ✅ |
| Claude Code | ❌ 只能 MCP/命令 | ✅ |
| Codex | ❌ | ✅（config 里注册） |
| opencode | ✅ 插件 | ✅ |

**结论：MCP 是唯一的公共面。** 所以交付物是：
1. 一个 **HTTP 服务**（`aio-searchd`，现 8792）——负责缓存、记账、出处、provider 路由；
2. 一个 **MCP server**（stdio，~150 行）——只做一件事：把 `search(q, top, fresh)` 转成一次 HTTP 调用。
   工具 schema 要极简（3 个参数），否则每个会话都要付上下文税。

## 二、三层结构

```
harness（CC/Codex/opencode/dsh）
   │  MCP(stdio) 或 provider 插件
   ▼
aio-searchd（127.0.0.1:8792）
   ├─ 缓存层：exact(sha1) + 语义近似(bigram 覆盖度) + 分层 TTL(fresh/official/default)
   ├─ 记账层：按 harness token 计次/计费；日预算 + 会话预算
   ├─ 出处层：每条结果必带 url + fetched_at + provider
   └─ provider 路由：见下
   ▼
providers
   ├─ dsh-plugin（现役主力，付费 ¥0.0055/次）
   ├─ Firecrawl（keyless，按 IP 限流）
   ├─ Tavily（keyless，交叉验证用）
   └─ agent-search CLI（keyless，兜底）
```

## 三、四个必须定死的接口

```http
GET  /search?q=&top=8&fresh=0&harness=cc&intent=auto
     → {ok, cached, backend, fetched_at, results:[{title,url,snippet}],
        cost:{calls,cny}, degraded:[...], elapsed_ms}
POST /record        # 任何 harness 把自己搜到的结果写回（只收带 url 的）
GET  /stats?days=7  # 按 harness/按 provider 的次数、命中率、成本、降级分布
GET  /health
```

**四条不可妥协的规则**：
1. **没有出处的条目不入库**（无 url 直接丢）；
2. **降级要有名字**（`provider-timeout` / `quota-exceeded` / `hygiene-dropped:n`），不是 `degraded:true`；
3. **成本按调用方记账**（harness token → 谁花的钱）；
4. **搜索词脱敏在客户端做**（沿用 `search-discipline`），服务端不负责猜哪些不能发。

## 三·补：核心已与宿主解耦（2026-11-08 落地）

`aio-search-core`（`dsh-plugins/dsh-web-search-plus/standalone.mjs`，:8793）已经跑起来：
**同一份 `lib/index.js`**，只把宿主的 5 个接入点（`ctx.get('credentials'|'agents')`、`ctx.web.register`）换成 `host.mjs` 的本地实现。
dsh 插件从此可以退化成 10 行适配器，**不再是必需品**。

对拍判据也踩了一次坑（详见 `eval/parity-core-vs-plugin.mjs` 头部）：
**不能用"结果 URL 集合"判等价** —— 上游检索 + LLM 改写天然非确定，噪声底在 0.00–1.00 之间跳。
改用三层：① 同一份实现（源码哈希相同）② 契约形状一致 ③ 可用性 + 不显著变慢。URL 重叠率只打印、不判定。
实测 5/5 通过，core p50 约 2.1s、插件路径约 4.3s（差在每次 spawn node）。

## 四、迁移路径（不搞大爆炸）

| 阶段 | 动作 | 判据 |
|---|---|---|
| P0（已做） | `aio-searchd` v0：只读 dsh 缓存 + `/record` 写回 | `/search` 命中 dsh 缓存，未命中明确返回 `cached:false` |
| **P1** | 把 dsh 的 journal 14 字段**统一写进 aio-searchd**（dsh 插件改成 POST /journal） | 一天内 `/stats` 能按 harness 出数 |
| P2 | MCP server 上线，先接 Claude Code（我自己天天用） | 同一条查询在 CC 与 dsh 命中同一份缓存 |
| P3 | provider 路由 + 配额：dsh-plugin 为主，keyless 兜底 | 超预算自动降级且日志可见 |
| P4 | 把 grounded 评测接到 `/stats`（离线跑，结果入库） | 每次改 provider 能对比 grounded rate |

## 五、风险（诚实版）

1. **keyless 是别人给的**：Firecrawl/Tavily 免费额度按 IP 限流，随时可能收紧 → 只能当备路，不能当主路。
2. **MCP 工具占上下文**：工具 schema 越大，每个会话越贵。**3 个参数、一句描述**是上限。
3. **服务单点**：`aio-searchd` 挂了，四个 harness 全瞎 → 必须有"直连 provider"的降级路径（每个 harness 保留自己的兜底）。
4. **隐私**：查询会发到外部服务；脱敏是客户端责任，服务端只做审计日志。
5. **不要把"多 provider"当卖点**：多引擎编排已经不是护城河（见 search-lab REPORT）。

## 六、与开源骨架的关系

本仓（`dsh-search-observability`）先发**口径与闸门**（journal 字段表 / 评测判据 / regress.sh），
底座代码（`aio-searchd` + MCP server）等 P1/P2 跑通后再决定是否单独开源——
**先证明它被四个 harness 真的用起来，再谈分享**。


---

## 附：宿主解耦记录（2026-11-08）

`host.mjs` 把宿主的 5 个接入点抽象成 `HostPorts`：
`get('credentials').resolve(ref)` / `get('agents').currentInitiator().session` / `web.registerSearchProvider()`。
`standalone.mjs` 用本地实现替换它们，得到 **aio-search-core:8793**（HTTP），
与 dsh 内运行的是**同一份 `lib/index.js`**（源码 sha1 相同即视为同一实现）。

三条要记住的边界：
1. **key 来源变化**：从 `~/.dsh/.credentials.yaml` 读 → 仍读该文件（`keysFromCredentialsFile`），但不再经 dsh 宿主；
2. **session 语义**：宿主的 `session.id` 用请求参数 `session` 替代，否则收敛/额度会退化成全局桶；
3. **状态外置**：standalone 故意关掉 guard/converge/cache/journal ——账本统一交给上层 aio-searchd，避免两套账。
