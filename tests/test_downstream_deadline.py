"""`run_downstream` 的**单账号墙钟上限**（防一个病态账号卡住整批）。

背景
====
2026-10-06 实测：对一个已注册但未激活的失败账号跑下游，
`BrowserSession.login(timeout=150, attempts=3, cooldown=15)` 最坏 ~8 分钟且
`as_completed` 要等**最慢**的那一个 ⇒ 11 个账号的跑批在 30 分钟处仍未返回，
**结果文件都没落盘**。本文件钉住修好后的行为：超时即放弃该账号、整批继续。

⚠ 用例**不联网、不起浏览器**：`run_one` 被打桩成可控的假件。
   模块加载方式与 `tests/test_downstream_divergence.py` 同源（那里有完整的
   "为什么不能用普通 import" 推导：`tools/` 不是包 + `_bootstrap` 会加载真实
   `.env`）。
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load_downstream_module(monkeypatch):
    """把 `tools/run_downstream.py` 当独立模块加载，不碰测试进程环境。

    实现与 `tests/test_downstream_divergence.py::_load_downstream_module` 一致
    （给 `_bootstrap` 打只带 `ROOT` 的桩，再用 `spec_from_file_location` 加载）。
    刻意**各自保留一份**而不是提取共享：那个函数是"隔离 `.env` 污染"这道护栏
    的一部分，让它跨文件复用它自己的加载语义会变得不明显。
    """
    boot = types.ModuleType("_bootstrap")
    boot.__dict__["ROOT"] = REPO
    monkeypatch.setitem(sys.modules, "_bootstrap", boot)

    path = REPO / "tools" / "run_downstream.py"
    spec = importlib.util.spec_from_file_location("_rd_deadline_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod(monkeypatch):
    return _load_downstream_module(monkeypatch)


def _call(mod, deadline, *, email="a@example.com"):
    return mod._run_deadline(
        {"email": email},
        deadline=deadline,
        headless=True,
        create=False,
        key_name="default",
        log=lambda _m: None,
    )


def test_hung_account_is_abandoned_at_the_deadline(mod, monkeypatch):
    """🔴 核心判据：卡住的账号**按时**被放弃，整批不再等它。"""
    release = threading.Event()

    def hung(*_a, **_k):
        # 模拟“永不返回”。用 `Event.wait` 而不是 `time.sleep`：前者语义上就是
        # “等一个不会来的信号”，也正是这类卡死的真实形态（等一个不会就绪的元素）。
        release.wait(30)

    monkeypatch.setattr(mod, "run_one", hung)
    t0 = time.time()
    out = _call(mod, 0.3)
    took = time.time() - t0

    assert out["downstream"].startswith("timeout")
    assert out["email"] == "a@example.com"
    assert took < 5, "没有在 deadline 处返回 —— 卡批行为又回来了"
    release.set()  # 放掉守护线程，别让它拖到进程退出


def test_fast_account_passes_through_unclipped(mod, monkeypatch):
    monkeypatch.setattr(
        mod, "run_one", lambda *_a, **_k: {"email": "a@example.com", "downstream": "ok"}
    )
    assert _call(mod, 60)["downstream"] == "ok"


def test_deadline_zero_disables_the_limit(mod, monkeypatch):
    """`--deadline 0` = 显式要旧行为（不设上限）。"""
    monkeypatch.setattr(
        mod, "run_one", lambda *_a, **_k: {"email": "a@example.com", "downstream": "ok"}
    )
    assert _call(mod, 0)["downstream"] == "ok"


def test_crash_inside_the_account_is_reported_not_raised(mod, monkeypatch):
    """账号内部抛异常 → 记成 `crash`，不能让整批崩。"""

    def boom(*_a, **_k):
        raise RuntimeError("browser exploded")

    monkeypatch.setattr(mod, "run_one", boom)
    out = _call(mod, 60)
    assert out["downstream"].startswith("crash: ")
    assert "browser exploded" in out["downstream"]


def test_abandoned_rows_are_recognised(mod):
    """🔴 汇总护栏：超时/崩溃的记录必须被识别为“未得出结论”。

    否则输入记录里的历史 `login_ms` 会把一个超时账号报成“登录成功”。
    """
    assert mod._abandoned({"downstream": "timeout: >60s"})
    assert mod._abandoned({"downstream": "crash: browser exploded"})
    assert not mod._abandoned({"downstream": "ok"})
    assert not mod._abandoned({"downstream": "login_failed: TimeoutError"})
    assert not mod._abandoned({})
    assert not mod._abandoned({"downstream": None})


def test_cli_exposes_the_deadline_flag(mod):
    """接线检查：`--deadline` 必须真的存在（参数被忽略是本项目点过名的缺陷）。"""
    # 直接扫源码里的 add_argument，避免为了拿 parser 去驱动整个 main()。
    src = (REPO / "tools" / "run_downstream.py").read_text(encoding="utf-8")
    assert '"--deadline"' in src
    # 默认值必须 > 0（默认不设上限 = 把旧 bug 当默认）。
    assert "default=300.0" in src
