"""槽位配置的**解析**与**记账映射**两条契约。

这个文件盯的是两个真实踩到的坑（2026-10-04，接 BYO 代理池时）：

坑 A：`config.proxy_slots()` 的注释解析
--------------------------------------
`proxy_slots()` 先把**全文**按逗号切分、再逐项判 `#` 前缀。于是注释行里只要
出现 ASCII 逗号，被切出来的碎片（不带 `#`）就会**被当成槽位 URL**：

    # 端口是 7131,7119,7149        ← 这行注释凭空产出 3 个假槽位

表现不是"多跑几个账号"，而是 `slot_scope()` 报「端口 7119 的出口 IP 未知」，
启动即失败 —— 且错误信息指向的是**注释里的数字**，不看代码根本猜不到来源。

坑 B：`config.slot_scope()` 用**端口**当配额记账 key
--------------------------------------------------
本地 mihomo 槽位（7901–7906）端口唯一，所以一直没暴露。但 BYO 远程代理的端口
是别人给的，会重复（实测一批 5 个里有 7131 ×2、7119 ×2）⇒ 两个**不同出口 IP**
映射到同一个 scope，配额记到别人头上。

`common/config.py` 自己的说明写着「scope 必须是唯一、不随配置漂移的标识」——
端口恰恰不唯一。`src/proxypool.py` 的 `_state_key()` 早就按 `host:port` 记账了，
`slot_scope()` 是漏掉的那一处。

跑法：
    pytest tests/test_slot_config.py -v
"""

import pytest

from common import config

# ══════════════════════════════════════════════════════════════════
# 坑 A —— 注释解析
# ══════════════════════════════════════════════════════════════════


def _use_slots_file(monkeypatch, tmp_path, text: str):
    """把 `proxy_slots()` 的来源指向一个临时文件。

    `IR_PROXY_SLOTS_FILE` 是 import 期常量，但 `proxy_slots()` 在**调用期**
    读它 ⇒ 用 `setattr` 打在 config 命名空间上（不是 `setenv`，见
    tests/test_config_override.py 的说明）。
    """
    f = tmp_path / "slots.txt"
    f.write_text(text, encoding="utf-8")
    monkeypatch.setattr(config, "IR_PROXY_SLOTS_FILE", str(f))
    monkeypatch.setattr(config, "IR_PROXY_SLOTS", "")


def test_comment_line_with_ascii_commas_yields_no_slots(monkeypatch, tmp_path):
    """🔴 注释行里的 ASCII 逗号**不能**造出槽位（坑 A 的本体）。"""
    _use_slots_file(
        monkeypatch, tmp_path, "# 这批代理的端口是 7131,7119,7149\nhttp://127.0.0.1:7901\n"
    )
    assert config.proxy_slots() == ["http://127.0.0.1:7901"]


def test_slot_file_formats_all_still_supported(monkeypatch, tmp_path):
    """回归护栏：一行一个 / 行内逗号分隔 / 注释 / 空行 / 前后空白。

    「修注释解析」很容易顺手把别的格式一起弄坏（比如只按行取、
    顺手丢掉逗号分隔），所以把公开承诺过的格式全钉一遍。
    """
    _use_slots_file(
        monkeypatch,
        tmp_path,
        "# 头部注释\n"
        "\n"
        "   http://127.0.0.1:7901   \n"
        "http://127.0.0.1:7902,http://127.0.0.1:7903\n"
        "# 尾部注释\n",
    )
    assert config.proxy_slots() == [
        "http://127.0.0.1:7901",
        "http://127.0.0.1:7902",
        "http://127.0.0.1:7903",
    ]


# ══════════════════════════════════════════════════════════════════
# 坑 B —— 记账映射
# ══════════════════════════════════════════════════════════════════


def test_slot_scope_distinguishes_same_port_on_different_hosts(monkeypatch):
    """🔴 端口相同、host 不同 ⇒ 必须是**两个** scope（坑 B 的本体）。"""
    monkeypatch.setattr(
        config,
        "SLOT_EGRESS_IPS",
        {
            "a.example.com:7119": "203.0.113.7",
            "b.example.com:7119": "203.0.113.8",
        },
    )
    assert config.slot_scope("https://a.example.com:7119") == "203.0.113.7"
    assert config.slot_scope("https://b.example.com:7119") == "203.0.113.8"


def test_duplicate_ports_across_hosts_get_distinct_scopes(monkeypatch, tmp_path):
    """🔴 端到端形态：5 个代理、端口有重复（7131×2、7119×2）⇒ 5 个不同 scope。

    这就是 2026-10-04 接入 BYO 代理池时真实的槽位清单。
    修复前 `slot_scope()` 会把 5 个槽位压成 3 个 scope（端口去重后）。
    """
    _use_slots_file(
        monkeypatch,
        tmp_path,
        "https://a.example.com:7131\n"
        "https://b.example.com:7119\n"
        "https://c.example.com:7149\n"
        "https://d.example.com:7119\n"
        "https://e.example.com:7131\n",
    )
    monkeypatch.setattr(
        config,
        "SLOT_EGRESS_IPS",
        {
            "a.example.com:7131": "203.0.113.1",
            "b.example.com:7119": "203.0.113.2",
            "c.example.com:7149": "203.0.113.3",
            "d.example.com:7119": "203.0.113.4",
            "e.example.com:7131": "203.0.113.5",
        },
    )
    scopes = [config.slot_scope(u) for u in config.proxy_slots()]
    assert scopes == ["203.0.113.1", "203.0.113.2", "203.0.113.3", "203.0.113.4", "203.0.113.5"]
    assert len(set(scopes)) == 5, "端口重复不能把两个出口压成一个 scope"


def test_bare_port_key_still_supported(monkeypatch):
    """兼容旧配置：`7901=<IP>` 继续生效（本地 mihomo 槽位就是这么写的）。

    没有这条回退，仓库里所有现存 `.env` 都会在升级后立刻跑不起来。
    """
    # ⚠ 用 TEST-NET-1（RFC 5737 保留段）作占位值 —— **不要**把真实出口 IP
    #   抄进测试：出口 IP 属于基础设施标识，进仓库等于贴出去（泄漏闸门会拦）。
    monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {"7901": "192.0.2.9"})
    assert config.slot_scope("http://127.0.0.1:7901") == "192.0.2.9"


def test_host_port_key_wins_over_bare_port(monkeypatch):
    """两种键都在时 `host:port` 优先 —— 它更具体，是新的推荐写法。"""
    monkeypatch.setattr(
        config, "SLOT_EGRESS_IPS", {"127.0.0.1:7901": "198.51.100.1", "7901": "203.0.113.9"}
    )
    assert config.slot_scope("http://127.0.0.1:7901") == "198.51.100.1"


def test_slot_scope_ignores_credentials_and_path(monkeypatch):
    """带账密 / 带 path / 带前后空白的写法都要能解析（BYO 代理就是这个形态）。"""
    monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {"h.example.com:7119": "203.0.113.7"})
    assert config.slot_scope("https://user:pass@h.example.com:7119") == "203.0.113.7"
    assert config.slot_scope(" https://h.example.com:7119/some/path ") == "203.0.113.7"


def test_unmapped_slot_raises_with_copyable_key_form(monkeypatch):
    """两种键都查不到 ⇒ 抛 `ValueError`，且报错要给出可直接照抄的写法。

    这条报错是"配置漏了一项"时唯一的线索（见 `slot_scope` docstring 里
    "宁可跑不起来也不静默错配"那段），所以它必须包含 `host:port` 形态。
    """
    monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {})
    with pytest.raises(ValueError) as ei:
        config.slot_scope("https://nope.example.com:9999")
    msg = str(ei.value)
    assert "nope.example.com:9999" in msg
    assert "IR_SLOT_EGRESS_IPS=nope.example.com:9999=" in msg
