"""探针：摸清 SSO 网关的 429 限流边界。

背景：4 路并发注册时 3 路被 429 拒绝。需要确定：
  1. 限流的是哪个接口（username/check 还是 register/byEmail）
  2. 限流是按 IP 的突发速率，还是并发连接数
  3. 429 响应里有没有 Retry-After 可依据

为安全起见用 `personal/username/check` 做探测 —— 它是只读的幂等接口，
不会产生任何账号。
"""
import io
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from _path import ROOT  # noqa: F401  （副作用：把 tools/ 与仓库根加进 sys.path）

from src import config  # noqa: E402

URL = f"{config.SSO_GW}/personal/username/check"


def one(i):
    """单次探测，返回 (i, status, retry_after, body_snippet, full_url)。"""
    import requests

    s = requests.Session()
    s.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "lang": "zh-CN",
        "Origin": config.SSO_BASE,
        "Referer": f"{config.SSO_BASE}/register",
        "User-Agent": config.USER_AGENT,
    })
    try:
        t = time.time()
        r = s.post(URL, json={"username": f"probe{i}{int(time.time()) % 100000}"},
                   timeout=20)
        dt = (time.time() - t) * 1000
        return (i, r.status_code, r.headers.get("Retry-After", "-"),
                r.text[:120].replace("\n", " "), r.url, round(dt))
    except Exception as ex:
        return (i, "ERR", "-", str(ex)[:100], URL, 0)


def burst(n, label):
    print(f"\n--- {label}: {n} 路同时发起 ---", flush=True)
    with ThreadPoolExecutor(max_workers=n) as ex:
        rows = list(ex.map(one, range(n)))
    ok = sum(1 for r in rows if r[1] == 200)
    print(f"  200: {ok}/{n}   429: {sum(1 for r in rows if r[1] == 429)}/{n}")
    for r in rows:
        print(f"    #{r[0]} status={r[1]} retry_after={r[2]} {r[5]}ms  {r[3][:80]}")
    return rows


def sequential(gap, n, label):
    print(f"\n--- {label}: 串行，间隔 {gap}s ---", flush=True)
    rows = []
    for i in range(n):
        rows.append(one(i))
        if i < n - 1:
            time.sleep(gap)
    ok = sum(1 for r in rows if r[1] == 200)
    print(f"  200: {ok}/{n}")
    for r in rows:
        print(f"    #{r[0]} status={r[1]} {r[5]}ms")
    return rows


if __name__ == "__main__":
    print(f"目标接口: {URL}")
    burst(2, "A 两路并发")
    time.sleep(8)
    burst(4, "B 四路并发")
    time.sleep(15)
    burst(8, "C 八路并发")
    time.sleep(20)
    sequential(0.3, 6, "D 串行 0.3s 间隔")
    time.sleep(15)
    sequential(1.0, 6, "E 串行 1.0s 间隔")
    print("\n完成")
