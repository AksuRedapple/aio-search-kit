#!/usr/bin/env python3
"""离线自测：不联网、不花钱，只验缓存/写回/配额/统计四条主路径。

用法：AIO_HOME=/tmp/aio-kit-test python3 tests/test_offline.py
判据：全部 PASS 且退出码 0。
"""
import json, os, subprocess, sys, time, urllib.error, urllib.request

HOME = os.environ.setdefault("AIO_HOME", "/tmp/aio-kit-test")
PORT = 8899
BASE = "http://127.0.0.1:%d" % PORT
ok = fail = 0

def check(name, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print("  ✅ %s%s" % (name, (" — " + detail) if detail else ""))
    else: fail += 1; print("  ❌ %s%s" % (name, (" — " + detail) if detail else ""))

def get(path):
    """返回 (status, body)；4xx/5xx 不抛，避免把"预期内的失败"当成测试崩溃。"""
    req = urllib.request.Request(BASE + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

def post(path, obj):
    req = urllib.request.Request(BASE + path, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

os.makedirs(HOME, exist_ok=True)
cfg = os.path.join(HOME, "config.json")
json.dump({"daily_max_calls": 2, "daily_max_cny": 1.0, "cost_per_call": 0.005,
           "providers": [{"name": "none", "type": "http", "url": "http://127.0.0.1:1/x"}]},
          open(cfg, "w"))

proc = subprocess.Popen([sys.executable, "aio_searchd.py"], env={**os.environ, "AIO_SEARCHD_PORT": str(PORT)},
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
time.sleep(1.5)
try:
    _, h = get("/health")
    check("health 可读", h.get("ok") is True, "port=%s" % h.get("port"))

    _, miss = get("/search?q=%E4%B8%8D%E5%AD%98%E5%9C%A8%E7%9A%84%E6%9F%A5%E8%AF%A2&harness=t")
    check("未命中返回 cached=false 且不报错", miss.get("cached") is False and miss.get("ok") is True)

    post("/record", {"query": "redis acl whoami", "sources": [
        {"title": "ACL WHOAMI", "url": "https://redis.io/docs/latest/commands/acl-whoami/", "snippet": "x"}]})
    _, hit = get("/search?q=redis%20acl%20whoami&harness=t")
    check("写回后可命中", hit.get("cached") is True and len(hit.get("results") or []) == 1)

    _, fuzzy = get("/search?q=redis%20acl%20whoami%20%E7%94%A8%E6%B3%95&harness=t")
    check("近似命中（容忍改写差异）", fuzzy.get("cached") is True, "score=%s" % fuzzy.get("match_score"))

    rc, rbody = post("/record", {"query": "无出处条目", "sources": [{"title": "no url"}]})
    check("无 url 的条目被拒收", rc == 400 and rbody.get("err") == "no-sources-with-url",
          "status=%s err=%s" % (rc, rbody.get("err")))

    _, s = get("/stats?days=1")
    check("stats 有 by_harness 与 quality 块", "by_harness" in s and "quality" in s)

    lc, live = get("/search?q=fresh-query-xyz&live=1&harness=t&origin=test")
    check("provider 不可用时报 no-provider（不 500）",
          lc == 502 and live.get("err") == "no-provider",
          "degraded=%s" % live.get("degraded"))
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill()

print("\n结果：%d 通过 / %d 失败" % (ok, fail))
sys.exit(0 if fail == 0 else 1)
