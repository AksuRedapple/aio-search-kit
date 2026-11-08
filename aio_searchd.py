#!/usr/bin/env python3
# aio-searchd —— 给 coding agent 用的**共享搜索底座**（缓存 / 配额 / 降级 / 统一 journal）
#
# 解决的问题：Claude Code / Codex / opencode / 任何 harness 各有一套搜索，
# 缓存、出处、成本互不相通 —— 同一问题两次不同答案，且谁的流量花了多少钱没人知道。
#
# 三个端点：
#   POST/GET /search?q=&top=&live=&harness=&origin=
#   POST     /record       把调用方自己搜到的结果写回（只收带 url 的）
#   GET      /stats        四问：花了多少 / 命中率 / 降级分布 / 按 provider 的 p50-p95
#   GET      /journal      原始记录
#   GET      /health
#
# 零第三方依赖（stdlib）。provider 由 config.json 决定，默认只依赖 keyless Firecrawl。
import hashlib, json, os, re, shutil, sqlite3, subprocess, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AIO_HOME = os.path.expanduser(os.environ.get("AIO_HOME", "~/.aio-searchd"))
DSH_DB = os.path.expanduser(os.environ.get("AIO_IMPORT_DB", ""))  # 可选：从别的 sqlite 缓存导入历史（默认关）
OWN_DB = os.path.join(AIO_HOME, "aio-searchd.db")
PORT = int(os.environ.get("AIO_SEARCHD_PORT", "8792"))


def norm(q: str) -> str:
    """查询归一化：小写、去标点、压空白——缓存命中的第一道闸。"""
    q = (q or "").lower()
    q = re.sub(r"[\s　]+", " ", q)
    q = re.sub(r"[，。、；：？！,.;:?!\"'“”‘’()（）\[\]【】]", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def bigrams(t: str):
    t = norm(t).replace(" ", "")
    return {t[i:i+2] for i in range(len(t)-1)} if len(t) > 1 else {t}


def sim(a: str, b: str) -> float:
    """字符 bigram Jaccard：中文没有空格分词，按二元组比更稳。"""
    A, B = bigrams(a), bigrams(b)
    if not A or not B:
        return 0.0
    if len(A & B) < 2:          # 只共享一个二元组不算命中（防短查询乱撞）
        return 0.0
    # 覆盖度（不是 Jaccard）：用户的短查询 vs dsh 的改写长查询，
    # 关心的是"我这句话有多少被这段检索覆盖"，所以除以较小的一侧。
    return len(A & B) / min(len(A), len(B))




# ── live provider：把 dsh 插件当后端（它已有 intent/改写/卫生/预算）──
CORE_URL = os.environ.get("AIO_SEARCH_CORE", "http://127.0.0.1:8793/search")
PLUGIN_BRIDGE = os.path.expanduser(os.environ.get("AIO_PLUGIN_BRIDGE", ""))  # 可选：外部检索桥（默认关）


def core_search(q: str, top: int, session: str = "searchd"):
    """首选：aio-search-core（无宿主依赖的同一份检索逻辑）。"""
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                CORE_URL, data=json.dumps({"query": q, "maxResults": top, "session": session}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read().decode())
            if d.get("ok"):
                return [{"title": x.get("title", ""), "url": x.get("url", ""),
                         "snippet": (x.get("snippet") or "")[:400]} for x in d.get("sources") or []]
        except Exception:
            time.sleep(0.5)
    return None


def live_search(q: str, top: int):
    """次选：插件桥（dsh 路径）。失败返回 None（不抛）。"""
    rows = core_search(q, top)
    if rows is not None:
        return rows
    if not os.path.exists(PLUGIN_BRIDGE):
        return None
    try:
        node = os.environ.get("AIO_NODE") or shutil.which("node") or os.path.expanduser(
            "~/.nvm/versions/node/v26.12.1/bin/node")
        if not os.path.exists(node):
            return None
        out = subprocess.run([node, PLUGIN_BRIDGE, q], capture_output=True, text=True, timeout=90)
        if out.returncode != 0:
            return None
        rows = json.loads(out.stdout or "[]")
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "snippet": (r.get("snippet") or "")[:400]} for r in rows if r.get("url")][:top]
    except Exception:
        return None


def journal_append(rec: dict):
    """统一 journal：任何 harness 的检索都落这一处（14 字段的子集，缺的留空）。"""
    path = os.path.expanduser("~/.aio-searchd/journal.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rec.setdefault("ts", int(time.time()))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")




# ── 配额与降级（P3）──────────────────────────────────────────────
# 每个 harness 一个 token；按 token 记日额度；超限自动降级到 keyless provider。
QUOTA_DIR = AIO_HOME
CONFIG_FILE = os.path.join(QUOTA_DIR, "config.json")


def cfg_list(key):
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f).get(key)
    except Exception:
        return None


def cfg(key, default):
    """配置优先级：config.json > 环境变量 > 默认。config.json 是为了不留 launchd env 残留。"""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if key in d:
            return type(default)(d[key])
    except Exception:
        pass
    return type(default)(os.environ.get("AIO_SEARCHD_" + key.upper(), default))
TOKENS_FILE = os.path.join(QUOTA_DIR, "tokens.json")
USAGE_FILE = os.path.join(QUOTA_DIR, "usage.json")

FIRECRAWL_JS = os.path.expanduser(os.environ.get("AIO_FIRECRAWL_JS", ""))


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def harness_token(harness: str) -> str:
    """给每个 harness 发一个本地 token（首次调用时生成）。用于额度归属，不是鉴权。"""
    toks = _load(TOKENS_FILE, {})
    if harness not in toks:
        toks[harness] = hashlib.sha256((harness + str(time.time())).encode()).hexdigest()[:24]
        _save(TOKENS_FILE, toks)
    return toks[harness]


def quota_state(harness: str) -> dict:
    usage = _load(USAGE_FILE, {})
    today = time.strftime("%Y-%m-%d")
    rec = usage.get(harness) or {}
    if rec.get("date") != today:
        rec = {"date": today, "calls": 0, "cny": 0.0}
    calls = rec.get("calls", 0); cny = float(rec.get("cny", 0.0))
    DAILY_MAX_CALLS = cfg("daily_max_calls", 200); DAILY_MAX_CNY = cfg("daily_max_cny", 2.0)
    return {"calls": calls, "limit": DAILY_MAX_CALLS,
            "left": max(0, DAILY_MAX_CALLS - calls),
            "cny": round(cny, 4), "cny_limit": DAILY_MAX_CNY,
            "cny_left": round(max(0.0, DAILY_MAX_CNY - cny), 4),
            "exceeded": calls >= DAILY_MAX_CALLS or cny >= DAILY_MAX_CNY}


def quota_charge(harness: str, n: int = 1):
    usage = _load(USAGE_FILE, {})
    today = time.strftime("%Y-%m-%d")
    rec = usage.get(harness) or {}
    if rec.get("date") != today:
        rec = {"date": today, "calls": 0, "cny": 0.0}
    rec["calls"] = rec.get("calls", 0) + n
    rec["cny"] = round(float(rec.get("cny", 0.0)) + n * cfg("cost_per_call", 0.00554), 6)
    usage[harness] = rec
    _save(USAGE_FILE, usage)


def keyless_search(q: str, top: int):
    """降级路径：keyless Firecrawl（MCP over stdio，需完整握手）。
    慢（~2–5s 启动）且按 IP 限流，只做兜底，不做主路。"""
    node = os.environ.get("AIO_NODE") or shutil.which("node") or os.path.expanduser(
        "~/.nvm/versions/node/v26.12.1/bin/node")
    if not os.path.exists(FIRECRAWL_JS) or not os.path.exists(node):
        return None
    try:
        # 用 node 显式跑，不要依赖 .bin 的 shebang（launchd 下 PATH 里没有 nvm 的 node）
        pr = subprocess.Popen([node, FIRECRAWL_JS], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except Exception:
        return None
    def send(obj):
        pr.stdin.write(json.dumps(obj) + "\n"); pr.stdin.flush()
    def read_until(mid, deadline=45):
        t0 = time.time()
        while time.time() - t0 < deadline:
            line = pr.stdout.readline()
            if not line: return None
            try: d = json.loads(line)
            except Exception: continue
            if d.get("id") == mid: return d
        return None
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "aio-searchd", "version": "0.2"}}})
        if read_until(1) is None: return None
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "firecrawl_search", "arguments": {"query": q, "limit": top}}})
        resp = read_until(2)
        if not resp: return None
        # 正确形状：result.content[0].text 是一段 JSON 字符串，里面有 data.web[]
        items = []
        try:
            for blk in (resp.get("result") or {}).get("content") or []:
                if blk.get("type") != "text": continue
                payload = json.loads(blk.get("text") or "{}")
                items.extend(((payload.get("data") or {}).get("web")) or [])
        except Exception:
            items = []
        rows = [{"title": it.get("title", ""), "url": it.get("url", ""),
                 "snippet": (it.get("description") or "")[:400]}
                for it in items if it.get("url")][:top]
        return rows or None
    except Exception:
        return None
    finally:
        try: pr.kill()
        except Exception: pass




# ── Provider 链（配置驱动，开源版默认只依赖 keyless Firecrawl）───────────
DEFAULT_PROVIDERS = [
    # 顺序即优先级；第一个成功就返回。type=http 打 JSON POST；type=mcp 走 stdio 工具调用。
    {"name": "firecrawl", "type": "mcp", "tool": "firecrawl_search",
     "cmd": ["{node}", "{firecrawl_js}"]},
]


def providers():
    p = cfg_list("providers")
    return p if p else DEFAULT_PROVIDERS


def http_provider(spec, q, top):
    body = {"query": q, "maxResults": top}
    req = urllib.request.Request(spec["url"], data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=int(spec.get("timeout", 60))) as r:
        d = json.loads(r.read().decode())
    rows = d.get("sources") or d.get("results") or []
    return [{"title": x.get("title", ""), "url": x.get("url", ""),
             "snippet": (x.get("snippet") or "")[:400]} for x in rows if x.get("url")][:top]


def mcp_provider(spec, q, top):
    """最小 MCP 客户端：initialize → initialized → tools/call，读回 content[].text 里的 JSON。"""
    node = cfg("node_bin", "node") or "node"
    cmd = [c.format(node=node, firecrawl_js=FIRECRAWL_JS) for c in spec["cmd"]]
    pr = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True)
    def send(o):
        pr.stdin.write(json.dumps(o) + "\n"); pr.stdin.flush()
    def wait(mid, deadline=45):
        t0 = time.time()
        while time.time() - t0 < deadline:
            line = pr.stdout.readline()
            if not line: return None
            try: d = json.loads(line)
            except Exception: continue
            if d.get("id") == mid: return d
        return None
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "aio-searchd", "version": "1"}}})
        if wait(1) is None: return None
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": spec.get("tool", "search"),
                         "arguments": {"query": q, "limit": top}}})
        resp = wait(2)
        if not resp: return None
        items = []
        for blk in (resp.get("result") or {}).get("content") or []:
            if blk.get("type") != "text": continue
            try:
                payload = json.loads(blk.get("text") or "{}")
            except Exception:
                continue
            items.extend(((payload.get("data") or {}).get("web")) or [])
        return [{"title": it.get("title", ""), "url": it.get("url", ""),
                 "snippet": (it.get("description") or "")[:400]} for it in items if it.get("url")][:top] or None
    finally:
        try: pr.kill()
        except Exception: pass


def chain_search(q, top, session="searchd"):
    """按配置顺序尝试所有 provider；返回 (rows, backend_name, degraded[])"""
    degraded = []
    for spec in providers():
        try:
            if spec.get("type") == "mcp":
                rows = mcp_provider(spec, q, top)
            else:
                rows = http_provider(spec, q, top)
            if rows:
                return rows, spec.get("name", "?"), degraded
            degraded.append("%s-empty" % spec.get("name", "?"))
        except Exception:
            degraded.append("%s-fail" % spec.get("name", "?"))
    return None, None, degraded


def init_own():
    db = sqlite3.connect(OWN_DB)
    db.execute("""CREATE TABLE IF NOT EXISTS rec(
        qkey TEXT PRIMARY KEY, query TEXT, sources TEXT, intent TEXT,
        harness TEXT, created_at INTEGER)""")
    db.commit()
    return db


def dsh_ro():
    if not os.path.exists(DSH_DB):
        return None
    try:
        return sqlite3.connect("file:%s?immutable=1" % DSH_DB, uri=True, timeout=2)
    except Exception:
        return None


def dsh_lookup(qkey: str, top: int):
    """在 dsh 缓存里按相似度找：返回最佳命中（≥0.55）并带 match_score。"""
    db = dsh_ro()
    if db is None:
        return None
    try:
        rows = db.execute("select payload, created_at from cache").fetchall()
    except Exception:
        return None
    best, best_score = None, 0.0
    for payload, created in rows:
        try:
            d = json.loads(payload)
        except Exception:
            continue
        qs = ((d.get("meta") or {}).get("queries") or [])
        for x in qs:
            sc = sim(qkey, x)
            if sc > best_score:
                best_score, best = sc, (d, created, x, qs)
    if not best or best_score < 0.70:
        return None
    d, created, matched, qs = best
    src = (d.get("sources") or [])[:top]
    return {
        "cached": True, "backend": "dsh-cache", "intent": d.get("intent"),
        "match_score": round(best_score, 2), "matched_query": matched,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created / 1000)),
        "queries": qs,
        "results": [{"title": s.get("title", ""), "url": s.get("url", ""),
                     "snippet": (s.get("snippet") or "")[:400]} for s in src],
    }


def own_lookup(qkey: str, top: int):
    db = init_own()
    row, best = None, 0.0
    for r in db.execute("select query, sources, intent, harness, created_at, qkey from rec"):
        sc = sim(qkey, r[5])
        if sc > best:
            best, row = sc, r
    if not row or best < 0.70:
        return None
    query, sources, intent, harness, created = row[0], row[1], row[2], row[3], row[4]
    src = json.loads(sources)[:top]
    return {"cached": True, "backend": "aio-searchd(%s)" % harness, "intent": intent,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created)),
            "queries": [query], "results": src}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _j(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path == "/health":
            n = 0
            db = dsh_ro()
            if db:
                try: n = db.execute("select count(*) from cache").fetchone()[0]
                except Exception: n = -1
            return self._j({"ok": True, "ver": 1, "dsh_entries": n, "port": PORT})
        if u.path == "/search":
            q = (qs.get("q") or [""])[0]
            top = int((qs.get("top") or ["8"])[0])
            fresh = (qs.get("fresh") or ["0"])[0] == "1"
            live = (qs.get("live") or ["0"])[0] == "1"
            harness = (qs.get("harness") or ["unknown"])[0][:32]
            origin = (qs.get("origin") or ["unknown"])[0][:16]
            if not q.strip():
                return self._j({"ok": False, "err": "q required"}, 400)
            k = norm(q)
            if not fresh:
                for f in (own_lookup, dsh_lookup):
                    r = f(k, top)
                    if r:
                        journal_append({"q": q, "harness": harness, "cached": True, "origin": origin,
                                        "backend": r.get("backend"), "ok": True,
                                        "results": len(r.get("results") or []),
                                        "ms": 0, "cny": 0.0, "degraded": []})
                        return self._j({"ok": True, "q": q, **r})
            if live:
                t0 = time.time()
                qs_ = quota_state(harness)
                degraded = []
                if qs_["exceeded"]:
                    degraded.append("quota-exceeded")
                rows, backend, d2 = chain_search(q, top, session=harness)
                degraded.extend(d2)
                if backend and rows and not qs_["exceeded"]:
                    quota_charge(harness)
                ms = int((time.time() - t0) * 1000)
                if not rows:
                    journal_append({"q": q, "harness": harness, "origin": origin, "cached": False,
                                    "backend": backend, "ok": False,
                                    "degraded": degraded + ["no-provider"], "ms": ms})
                    return self._j({"ok": False, "q": q, "cached": False, "backend": backend,
                                    "results": [], "err": "no-provider", "degraded": degraded, "ms": ms}, 502)
                db = init_own()
                db.execute("INSERT OR REPLACE INTO rec VALUES (?,?,?,?,?,?)",
                           (k, q, json.dumps(rows, ensure_ascii=False), "fact", harness, int(time.time())))
                db.commit()
                journal_append({"q": q, "harness": harness, "origin": origin, "cached": False,
                                "backend": backend, "ok": True, "results": len(rows),
                                "degraded": degraded, "ms": ms,
                                "cny": round(cfg("cost_per_call", 0.0), 6),
                                "quota": quota_state(harness)})
                return self._j({"ok": True, "q": q, "cached": False, "backend": backend,
                                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "results": rows, "ms": ms, "degraded": degraded,
                                "quota": quota_state(harness)})
            journal_append({"q": q, "harness": harness, "origin": origin, "cached": False, "backend": None,
                            "ok": False, "degraded": ["cache-miss"]})
            return self._j({"ok": True, "q": q, "cached": False, "backend": None,
                            "results": [],
                            "hint": "未命中；加 &live=1 走实时检索，或由调用方取回后 POST /record 写回"})
        if u.path == "/journal":
            path = os.path.expanduser("~/.aio-searchd/journal.jsonl")
            rows = []
            if os.path.exists(path):
                for line in open(path, encoding="utf-8"):
                    line=line.strip()
                    if not line: continue
                    try: rows.append(json.loads(line))
                    except Exception: pass
            by = {}
            for r in rows:
                h = r.get("harness", "unknown")
                d = by.setdefault(h, {"n": 0, "ok": 0, "cached": 0, "ms": []})
                d["n"] += 1
                d["ok"] += 1 if r.get("ok") else 0
                d["cached"] += 1 if r.get("cached") else 0
                if isinstance(r.get("ms"), int): d["ms"].append(r["ms"])
            for h, d in by.items():
                ms = sorted(d.pop("ms")); d["p50_ms"] = ms[len(ms)//2] if ms else None
            return self._j({"ok": True, "total": len(rows), "by_harness": by})
        if u.path == "/stats":
            path = os.path.expanduser("~/.aio-searchd/journal.jsonl")
            rows = []
            if os.path.exists(path):
                for line in open(path, encoding="utf-8"):
                    line = line.strip()
                    if not line: continue
                    try: rows.append(json.loads(line))
                    except Exception: pass
            days = int((qs.get("days") or ["7"])[0])
            only_origin = (qs.get("origin") or [None])[0]
            since = time.time() - days * 86400
            rows = [r for r in rows if r.get("ts", 0) >= since]
            if only_origin:
                rows = [r for r in rows if r.get("origin") == only_origin]
            def pct(xs, p):
                xs = sorted(xs)
                return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else None
            out = {}
            for r in rows:
                h = r.get("harness", "unknown")
                d = out.setdefault(h, {"n": 0, "cached": 0, "ok": 0, "cny": 0.0,
                                       "degraded": {}, "by_backend": {}})
                d["n"] += 1
                d["cached"] += 1 if r.get("cached") else 0
                d["ok"] += 1 if r.get("ok") else 0
                d["cny"] = round(d["cny"] + float(r.get("cny") or 0), 6)
                for g in (r.get("degraded") or []):
                    d["degraded"][g] = d["degraded"].get(g, 0) + 1
                b = d["by_backend"].setdefault(r.get("backend") or "none", {"n": 0, "ms": []})
                b["n"] += 1
                if isinstance(r.get("ms"), int): b["ms"].append(r["ms"])
            for h, d in out.items():
                d["hit_rate"] = round(d["cached"] / d["n"], 3) if d["n"] else 0
                d["ok_rate"] = round(d["ok"] / d["n"], 3) if d["n"] else 0
                for b, v in d["by_backend"].items():
                    v["p50_ms"] = pct(v["ms"], 0.5); v["p95_ms"] = pct(v["ms"], 0.95)
                    v.pop("ms", None)
            # 质量块：把 eval 的 grounded 结果按 qkey 与过程数据对齐
            qpath = os.path.join(QUOTA_DIR, "quality.jsonl")
            qrows = []
            if os.path.exists(qpath):
                for line in open(qpath, encoding="utf-8"):
                    line = line.strip()
                    if not line: continue
                    try: qrows.append(json.loads(line))
                    except Exception: pass
            qrows = [r for r in qrows if r.get("ts", 0) >= since]
            jkeys = set()
            for r in rows:
                jkeys.add(hashlib.sha1(" ".join(str(r.get("q", "")).lower().split()).encode()).hexdigest()[:16])
            joined = [r for r in qrows if r.get("qkey") in jkeys]
            quality = {
                "records": len(qrows),
                "grounded_ok": sum(1 for r in qrows if r.get("ok")),
                "grounded_rate": round(sum(1 for r in qrows if r.get("ok")) / len(qrows), 3) if qrows else None,
                "joined_with_runtime": len(joined),
                "join_rate": round(len(joined) / len(qrows), 3) if qrows else None,
            }
            origins = {}
            for r in rows:
                o = r.get("origin", "unknown")
                origins[o] = origins.get(o, 0) + 1
            return self._j({"ok": True, "days": days, "total": len(rows),
                            "by_origin": origins, "by_harness": out, "quality": quality})
        return self._j({"ok": False, "err": "not found"}, 404)

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/record":
            return self._j({"ok": False, "err": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._j({"ok": False, "err": "bad json"}, 400)
        q = (body.get("query") or "").strip()
        src = body.get("sources") or []
        if not q or not isinstance(src, list):
            return self._j({"ok": False, "err": "query/sources required"}, 400)
        # 只收带出处的条目：没有 url 的结果入库等于制造"没出处"的记忆
        clean = [{"title": s.get("title", ""), "url": s.get("url", ""),
                  "snippet": (s.get("snippet") or "")[:400]} for s in src if s.get("url")]
        if not clean:
            # 没有出处的条目不入库——否则等于往缓存里塞"没证据的记忆"
            return self._j({"ok": False, "err": "no-sources-with-url"}, 400)
        db = init_own()
        db.execute("INSERT OR REPLACE INTO rec VALUES (?,?,?,?,?,?)",
                   (norm(q), q, json.dumps(clean, ensure_ascii=False),
                    body.get("intent") or "fact", body.get("harness") or "unknown", int(time.time())))
        db.commit()
        return self._j({"ok": True, "stored": len(clean), "key": norm(q)})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    init_own()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print("aio-searchd on 127.0.0.1:%d  dsh_db=%s" % (PORT, DSH_DB), flush=True)
    srv.serve_forever()
