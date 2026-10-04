"""配置有**两套**覆盖机制 —— 这条契约必须显式钉住，否则混用时静默失效。

背景（2026-09-20 审计发现）
---------------------------
本项目的配置覆盖走两条**完全不同**的路：

| 机制 | 例子 | 正确的覆盖方式 |
|---|---|---|
| **import 期固化** | `config.REG_QUOTA_MAX` / `REG_QUOTA_WINDOW_H` / `SLOT_EGRESS_IPS`、`browser.constants` 的 10 个常量 | `monkeypatch.setattr(config, "X", v)` |
| **调用期读 env** | `quota.state_path()` / `proxypool.state_path()` | `monkeypatch.setenv("IR_QUOTA_STATE", …)` |

🔴 混用的后果是**静默失效**：`config.py` 有 17 处 `os.getenv` 在**模块顶层**，
值在 `import common.config` 那一刻就定死了。此后 `monkeypatch.setenv("IR_REG_QUOTA_MAX", "5")`
**不报错、不生效** —— 用例照样跑完，只是测的不是它以为的那个上限。

⚠ 当前**没有 bug**：仓库里 11 处 `setattr(config, …)` 全部用对了。这条测试不是
"修缺陷"，是**装保险丝** —— 把当前语义写死，让未来的改动必须是有意的。

跑法：
    pytest tests/test_config_override.py -v
"""

from pathlib import Path

import pytest

from common import config
from src import proxypool, quota


# ── 机制一：import 期固化 ─────────────────────────────────────────────
def test_import_time_constants_ignore_setenv(monkeypatch):
    """🔴 `setenv` 对 import 期常量**无效** —— 这正是那个陷阱本身。

    断言的是"无效"这个事实。如果哪天它变成了"有效"，说明有人把 `config` 改成
    调用期读取了 —— 那时**必须同步改这条断言**，以及仓库里 11 处
    `monkeypatch.setattr(config, …)`（它们会变成多余的，但不会报错）。
    """
    before = config.REG_QUOTA_MAX
    monkeypatch.setenv("IR_REG_QUOTA_MAX", str(before + 12345))
    assert config.REG_QUOTA_MAX == before, (
        "config.REG_QUOTA_MAX 竟然响应了 setenv —— 语义变了。"
        "要么改回去，要么把这条断言与 11 处 setattr 调用点一起更新。"
    )


def test_import_time_constants_respond_to_setattr(monkeypatch):
    """机制一的**正确用法**：`setattr` 打在 `config` 命名空间上。"""
    monkeypatch.setattr(config, "REG_QUOTA_MAX", 5)
    assert config.REG_QUOTA_MAX == 5


def test_window_constant_is_also_import_time(monkeypatch):
    """换一个常量再验一次，避免"只有某个常量恰好是函数"的巧合。"""
    before = config.REG_QUOTA_WINDOW_H
    monkeypatch.setenv("IR_REG_QUOTA_WINDOW_H", "999")
    assert config.REG_QUOTA_WINDOW_H == before


# ── 机制二：调用期读 env ──────────────────────────────────────────────
def test_quota_state_path_reads_env_at_call_time(monkeypatch):
    """`quota.state_path()` 是**函数** ⇒ `setenv` 当场生效（与机制一相反）。

    它的 docstring 写明了"为什么用函数而不是模块常量"：`config` 在 import 时
    就把环境变量读完了，而测试要能在同一个进程里把状态重定向到临时文件。
    """
    monkeypatch.setenv("IR_QUOTA_STATE", "sentinel_quota.jsonl")
    assert quota.state_path().name == "sentinel_quota.jsonl"


def test_proxypool_state_path_reads_env_at_call_time(monkeypatch):
    """同上，池子状态文件。这条同时保护 conftest 的 autouse 隔离有效。"""
    monkeypatch.setenv("IR_PROXY_STATE", "sentinel_pool.json")
    assert proxypool.state_path().name == "sentinel_pool.json"


# ── 两条机制不得被"顺手统一" ──────────────────────────────────────────
@pytest.mark.parametrize("mod,fn_name", [(quota, "state_path"), (proxypool, "state_path")])
def test_state_path_stays_a_function(mod, fn_name):
    """🔴 把 `state_path` 改成模块常量的后果是**测试隔离静默失效**。

    它一旦变成常量，`conftest.py` 的 autouse 夹具（靠 `setenv` 指走状态文件）
    就不再生效 ⇒ 测试会去读写**用户的真实运行态**：
      - 池子状态被假槽位覆盖 ⇒ 下次跑批的退避序列从第一档重来；
      - 配额台账被测试写入 ⇒ 真实计数被污染。
    两者都不报错，症状要到很久以后才显形。所以这里钉住"它必须是函数"。
    """
    assert callable(getattr(mod, fn_name)), (
        f"{mod.__name__}.{fn_name} 必须是可调用对象（函数），不能改成模块常量 —— "
        "否则 conftest 的 setenv 隔离会静默失效，测试会碰到真实运行态文件。"
    )


def test_conftest_isolation_actually_takes_effect():
    """兜底：证明 autouse 夹具真的把两个状态文件指到了 tmp 目录。

    ⚠ 这条依赖 `conftest._isolate_runtime_state`。它同时是上面那条断言
    的"为什么" —— 隔离靠的就是 `setenv` + `state_path()` 是函数这个组合。
    """
    for p in (quota.state_path(), proxypool.state_path()):
        assert Path(p).is_absolute(), f"{p} 不是绝对路径，隔离可能没生效"
        assert ".workbuddy-ai" not in str(p), (
            f"{p} 指向了仓库内的运行态目录 —— autouse 隔离没生效，"
            "测试会读写用户的真实状态文件。"
        )
