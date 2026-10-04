"""`tools/data/migrate_quota_scope.py` 的出口换算必须跟 `config.slot_scope()` **同一套规则**。

背景（2026-10-04）
------------------
`config.slot_scope()` 改成按 `host:port` 记账之后，这个工具还在只拿**端口**去查
`SLOT_EGRESS_IPS`：

    m = _PORT_RE.search("slot2(https://a.example.com:7119)")
    ip = config.SLOT_EGRESS_IPS.get(m.group(1))        # 查的是 "7119"

而新的键是 `a.example.com:7119` ⇒ 查不到 ⇒ 记进 `unknown_port` ⇒ **静默跳过**。

为什么这条必须测：跳过是**安全方向**（"宁可少迁，不可迁错"），所以它不会报错、
不会崩，只会让迁移报告看起来一切正常 —— 而实际上 `host:port` 键的槽位**一条都没迁**。
台账于是继续挂着错的 scope，且没人会发现。

⚠ 这里断言的是"工具与 `config.slot_scope()` 结论一致"，不是"工具里有一份自己的
   查表逻辑" —— 端口重复（远程代理的常态）时，两份逻辑必然分叉。

跑法：
    pytest tests/test_migrate_quota_scope.py -v
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from common import config

REPO = Path(__file__).resolve().parents[1]


def _load_module(monkeypatch):
    """把 `tools/data/migrate_quota_scope.py` 当**独立模块**加载。

    与 `tests/test_downstream_divergence.py` 同一套理由：`tools/` 刻意不是包
    （没有 `__init__.py`），而它顶部的 `from _path import ROOT` 是**全局副作用**
    （会改 `sys.path`）。所以给 `_path` 打一个只带 `ROOT` 的桩，随 `monkeypatch` 回滚。
    """
    shim = types.ModuleType("_path")
    shim.__dict__["ROOT"] = REPO
    monkeypatch.setitem(sys.modules, "_path", shim)

    path = REPO / "tools" / "data" / "migrate_quota_scope.py"
    spec = importlib.util.spec_from_file_location("_migrate_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_results(tmp_path: Path, records: list[dict]) -> Path:
    p = tmp_path / "results.json"
    p.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return p


def test_resolves_slot_registered_by_host_port(monkeypatch, tmp_path):
    """🔴 `SLOT_EGRESS_IPS` 用 `host:port` 键时也必须能换算出来。"""
    monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {"a.example.com:7119": "203.0.113.7"})
    results = _write_results(
        tmp_path,
        [
            {"email": "u@example.com", "proxy_slot": "slot2(https://a.example.com:7119)"},
        ],
    )
    assert _load_module(monkeypatch).load_email_to_ip(results) == {"u@example.com": "203.0.113.7"}


def test_duplicate_ports_on_different_hosts_stay_distinct(monkeypatch, tmp_path):
    """🔴 端口相同、host 不同 ⇒ 必须各归各的出口（不能靠端口分派）。

    这批就是 2026-10-04 接入的 BYO 代理形态：7131 与 7119 各出现两次。
    """
    monkeypatch.setattr(
        config,
        "SLOT_EGRESS_IPS",
        {
            "a.example.com:7119": "203.0.113.7",
            "b.example.com:7119": "203.0.113.8",
            "c.example.com:7131": "203.0.113.9",
        },
    )
    results = _write_results(
        tmp_path,
        [
            {"email": "a@example.com", "proxy_slot": "slot1(https://a.example.com:7119)"},
            {"email": "b@example.com", "proxy_slot": "slot2(https://b.example.com:7119)"},
            {"email": "c@example.com", "proxy_slot": "slot3(https://c.example.com:7131)"},
        ],
    )
    assert _load_module(monkeypatch).load_email_to_ip(results) == {
        "a@example.com": "203.0.113.7",
        "b@example.com": "203.0.113.8",
        "c@example.com": "203.0.113.9",
    }


def test_legacy_bare_port_key_still_resolves(monkeypatch, tmp_path):
    """兼容：本地 mihomo 时代的 `7901=<IP>` 写法仍要能换算（历史台账靠它）。"""
    # ⚠ 占位值用 TEST-NET-1（RFC 5737 保留段）—— 真实出口 IP 不进仓库。
    monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {"7901": "192.0.2.9"})
    results = _write_results(
        tmp_path,
        [
            {"email": "old@example.com", "proxy_slot": "slot1(http://127.0.0.1:7901)"},
        ],
    )
    assert _load_module(monkeypatch).load_email_to_ip(results) == {"old@example.com": "192.0.2.9"}


def test_slot_url_with_path_suffix_resolves_by_host_port(monkeypatch, tmp_path):
    """URL 带 path 后缀（临时踩过的工作区形态）也要能换算 —— path 不参与记账。"""
    monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {"h.example.com:7131": "203.0.113.7"})
    results = _write_results(
        tmp_path,
        [
            {"email": "p@example.com", "proxy_slot": "slot1(https://h.example.com:7131/a)"},
        ],
    )
    assert _load_module(monkeypatch).load_email_to_ip(results) == {"p@example.com": "203.0.113.7"}


def test_unmapped_slot_is_skipped_and_reported_as_host_port(monkeypatch, tmp_path, capsys):
    """映射缺失 ⇒ **跳过而不是猜**，且报出的键要能直接拷进 `.env`。

    报 `host:port` 而不是端口：端口在远程代理上不唯一，照着它补映射会补错。
    """
    monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {})
    results = _write_results(
        tmp_path,
        [
            {"email": "x@example.com", "proxy_slot": "slot1(https://a.example.com:7119)"},
        ],
    )
    assert _load_module(monkeypatch).load_email_to_ip(results) == {}
    out = capsys.readouterr().out
    assert "a.example.com:7119" in out


def test_read_ledger_aborts_loudly_on_corrupt_line(monkeypatch, tmp_path):
    """坏行 ⇒ 当场失败并**指出行号**，绝不静默跳过。

    跳过等于把那一行的账号配额记录从台账里默默删掉（`main()` 会原样写回去）。
    """
    ledger = tmp_path / "q.jsonl"
    ledger.write_text('{"email": "ok@example.com", "scope": ""}\n这不是 JSON\n', encoding="utf-8")
    mod = _load_module(monkeypatch)
    with pytest.raises(SystemExit) as ei:
        mod.read_ledger(ledger)
    assert "第 2 行" in str(ei.value)
