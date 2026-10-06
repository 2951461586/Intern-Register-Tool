"""注册阶段的**出口级复用与限速**（`_RateLimiter` / `_SsoPool` / `WafCookieCache`）。

为什么单独一个文件
==================
2026-10-05 近三轮实测（428 账号 / 81 失败）暴露出两处“单位不一致”：

  1. 限速是**全局**的（一个 1.2s 闸门串起所有出口），而服务端的限流/配额是
     **按出口 IP** 的 ⇒ 5 个槽位被压成 1 个出口的速率。
  2. 解盾结果**每账号重解一次**（每次启一个真实 Chrome，~5–10s）——
     而挑战是按出口下发的，同一出口的下一个账号本可复用。

这里钉住修好后的行为。⚠ 全程**不联网、不启浏览器**：`_SsoPool` 的 `factory`
可注入，`time.sleep` 在用例里被替身掉。
"""

from __future__ import annotations

import time
from typing import Any

from src.pipeline import _RateLimiter, _SsoPool
from src.sso import WafCookieCache, waf_state_path

# ── `_RateLimiter`：按 scope 各自限速 ─────────────────────────────


def test_different_scopes_do_not_block_each_other(monkeypatch):
    """🔴 判别力：不同出口各自限速 —— 一个出口在等，不该拖住另一个出口。"""
    sleeps: list = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    rl = _RateLimiter(0.2)

    rl("203.0.113.7")
    rl("203.0.113.8")

    assert sleeps == [], "两个不同 scope 的**首次**调用都不该等"


def test_same_scope_still_enforces_the_min_interval(monkeypatch):
    """同一出口仍守最小间隔 —— 收窄的是“跨出口”，不是把闸门拆掉。"""
    sleeps: list = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    rl = _RateLimiter(0.2)

    rl("203.0.113.7")
    rl("203.0.113.7")

    assert len(sleeps) == 1
    # ⚠ 上界留余量：`target + min_interval` 在 1.79e9 量级的浮点上会掉精度
    #   （实测 0.2000000476），不是行为问题。
    assert 0 < sleeps[0] <= 0.25


def test_empty_scope_is_a_single_bucket(monkeypatch):
    """`scope=""`（非槽位模式）退化成**单桶**，与加 scope 之前行为一致。"""
    sleeps: list = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    rl = _RateLimiter(0.2)

    rl()
    rl()

    assert len(sleeps) == 1


def test_reservation_happens_under_lock_but_sleep_happens_outside():
    """两个 scope 并发时，第二个不该被第一个的**睡眠**挡住。

    判据：两个线程各自首次调用，墙钟应远小于一次 min_interval 的等待 ——
    抱着锁 sleep 的旧实现会让第二个等到第一个睡完。这里只用一次 0.2s 的
    间隔，断言总耗时 < 0.35s（留足调度余量）。
    """
    rl = _RateLimiter(0.5)
    done: list = []

    def call(scope):
        rl(scope)
        done.append(scope)

    import threading

    t1 = threading.Thread(target=call, args=("a",))
    t2 = threading.Thread(target=call, args=("b",))
    t0 = time.time()
    t1.start(), t2.start()
    t1.join(), t2.join()
    assert sorted(done) == ["a", "b"]
    assert time.time() - t0 < 0.35, "不同 scope 的睡眠被串起来了（锁没放对）"


# ── `_SsoPool`：按出口复用 client ────────────────────────────────


def _recording_factory(created: list):
    def factory(*, proxy: Any | None = None, waf_cache: Any | None = None) -> Any:
        created.append(proxy)
        return object()

    return factory


def test_pool_reuses_one_client_per_exit():
    created: list = []
    pool = _SsoPool(waf_cache=WafCookieCache(), factory=_recording_factory(created))

    a1 = pool.get("http://127.0.0.1:7901")
    a2 = pool.get("http://127.0.0.1:7901")
    b = pool.get("http://127.0.0.1:7902")

    assert a1 is a2, "同一出口必须复用（WAF cookie / 连接都跟着复用）"
    assert b is not a1, "不同出口不能共用 client（cookie 是按出口下发的）"
    assert created == ["http://127.0.0.1:7901", "http://127.0.0.1:7902"]


def test_pool_does_not_share_a_session_in_single_exit_mode():
    """🔴 单出口模式**不**复用 client：多个 producer 会共用同一个
    `requests.Session`，而它的 cookie jar / 连接池没有跨线程保证。
    解盾复用仍由共享的 `WafCookieCache` 负责（只共享 cookie 值）。"""
    created: list = []
    pool = _SsoPool(waf_cache=WafCookieCache(), factory=_recording_factory(created))

    assert pool.get(None) is not pool.get(None)
    assert created == [None, None]


def test_pool_passes_the_shared_cache_to_every_client():
    """单出口模式下 cookie 复用**必须**靠共享 cache（client 不复用）。"""
    seen: list = []
    cache = WafCookieCache()

    def factory(*, proxy: Any | None = None, waf_cache: Any | None = None) -> Any:
        seen.append(waf_cache)
        return object()

    pool = _SsoPool(waf_cache=cache, factory=factory)
    pool.get(None)
    pool.get("http://127.0.0.1:7901")

    assert seen == [cache, cache]


# ── `WafCookieCache`：可选落盘（跨批次复用）──────────────────────


def test_cookie_survives_a_new_process_instance(tmp_path):
    """🔴 核心判据：解盾结果必须能跨 `run.py` 进程复用。

    内存缓存的寿命 = 一个进程，而每批 `run.py` 都是新进程 ⇒ 不落盘就要
    每批每出口重解一次（实测 ~8.8s × 5 出口 ≈ 44s/批）。
    """
    p = tmp_path / "waf.json"
    WafCookieCache(path=p).set("http://127.0.0.1:7901", "ACW-X")

    fresh = WafCookieCache(path=p)  # 模拟“下一个进程”
    assert fresh.get("http://127.0.0.1:7901") == "ACW-X"
    assert fresh.get("http://127.0.0.1:7902") == ""


def test_state_file_holds_one_entry_per_exit(tmp_path):
    p = tmp_path / "waf.json"
    c = WafCookieCache(path=p)
    c.set("127.0.0.1:7901", "A")
    c.set("127.0.0.1:7902", "B")
    back = WafCookieCache(path=p)
    assert (back.get("127.0.0.1:7901"), back.get("127.0.0.1:7902")) == ("A", "B")


def test_unchanged_value_does_not_rewrite_the_file(tmp_path, monkeypatch):
    """同一出口重复解盾（cookie 一样）不该反复写盘。"""
    from src import sso as sso_mod

    calls: list = []
    monkeypatch.setattr(
        sso_mod.fsutil, "atomic_write_text", lambda path, text, **k: calls.append(path) or path
    )
    c = sso_mod.WafCookieCache(path=tmp_path / "waf.json")
    c.set("k", "ACW")
    c.set("k", "ACW")

    assert len(calls) == 1


def test_corrupt_state_file_is_treated_as_empty_and_warns(tmp_path):
    """坏文件不能拖死注册 —— 降级为空缓存 + 一条告警。"""
    p = tmp_path / "waf.json"
    p.write_text("{not json", encoding="utf-8")
    warns: list = []

    c = WafCookieCache(path=p, log=warns.append)

    assert c.get("k") == ""
    assert warns and "读不出" in warns[0]


def test_persist_failure_warns_instead_of_raising(tmp_path):
    """写盘失败只能降级成告警（状态文件是加速器，不是正确性依赖）。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")  # 用一个**文件**占住父目录位置
    warns: list = []

    c = WafCookieCache(path=blocker / "waf.json", log=warns.append)
    c.set("k", "ACW")  # 不得抛

    # ⚠ **不能**断言 `warns[0]`：`__init__` 的 `_load()` 对这个路径会**先**失败一次，
    #    而它的异常类型**依平台而异** —— Linux 给 `NotADirectoryError`（记一条
    #    「读不出」），Windows 给 `FileNotFoundError`（被静默跳过）。
    #    CI（Linux）就是这样红的（2026-10-06）。只断言“写不回”这条出现过。
    assert any("写不回" in w for w in warns)


def test_missing_state_file_is_silent(tmp_path):
    """首次运行（文件还不存在）不该报警。"""
    warns: list = []
    assert WafCookieCache(path=tmp_path / "nope.json", log=warns.append).get("k") == ""
    assert warns == []


def test_state_path_honours_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("IR_WAF_STATE", str(tmp_path / "x.json"))
    assert waf_state_path() == tmp_path / "x.json"


def test_state_path_defaults_under_the_gitignored_state_dir(monkeypatch):
    monkeypatch.delenv("IR_WAF_STATE", raising=False)
    parts = waf_state_path().parts
    assert parts[-3:] == (".workbuddy-ai", "state", "waf_cookies.json")
