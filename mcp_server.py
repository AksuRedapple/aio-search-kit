#!/usr/bin/env python3
"""aio-search MCP server（stdio，stdlib only）

一个工具：search(query, top=8, live=false)
  → 打本机 aio-searchd（127.0.0.1:8792）；未命中默认不花钱，live=true 才走实时检索。

为什么要它：MCP 是 Claude Code / Codex / opencode / dsh 的唯一公共面。
工具 schema 刻意极小（3 个参数），否则每个会话都要付上下文税。
"""
import json, os, sys, urllib.parse, urllib.request

ENDPOINT = os.environ.get("AIO_SEARCHD_URL", "http://127.0.0.1:8792/search")
TOOL = {
    "name": "search",
    "description": "本机共享搜索（带缓存与出处）。未命中时加 live=true 才走实时检索（会计费）。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索词"},
            "top": {"type": "integer", "default": 8},
            "live": {"type": "boolean", "default": False},
        },
        "required": ["query"],
    },
}


def call_search(args):
    q = str(args.get("query") or "").strip()
    if not q:
        return {"error": "query required"}
    params = {
        "q": q,
        "top": str(int(args.get("top") or 8)),
        "live": "1" if args.get("live") else "0",
        "harness": "mcp",
    }
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return {"error": "searchd-unreachable", "detail": str(e)[:120]}
    rows = data.get("results") or []
    if not rows:
        return {"content": [{"type": "text", "text": "未命中（本机缓存没有；可加 live=true 走实时检索）"}]}
    lines = []
    for i, r in enumerate(rows, 1):
        lines.append("[%d] %s\n%s\n%s" % (i, r.get("title", ""), r.get("url", ""), (r.get("snippet") or "")[:200]))
    head = "backend=%s cached=%s\n\n" % (data.get("backend"), data.get("cached"))
    return {"content": [{"type": "text", "text": head + "\n\n".join(lines)}]}


def handle(req):
    mid = req.get("id")
    method = req.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "aio-search", "version": "0.1"},
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL]}}
    if method == "tools/call":
        p = req.get("params") or {}
        if p.get("name") != "search":
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown tool"}}
        res = call_search(p.get("arguments") or {})
        if "error" in res:
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "isError": True, "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}]}}
        return {"jsonrpc": "2.0", "id": mid, "result": res}
    if method in ("notifications/initialized", "initialized"):
        return None
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
