"""pytest 全局夹具 —— 把**运行态文件**重定向到 pytest 的临时目录。

为什么必须 autouse
------------------
`ProxySlotPool` 一构造就读状态文件（`_load_state`），一 `report_banned` /
`report_failed` 就写状态文件（`_save_state_locked`）。测试里构造的池子用的是
**假槽位**（`127.0.0.1:7901` 之类），放任它读写真实路径会有两种坏结果：

  ① 测试之间互相污染 —— 上一个用例留下的冷却会影响下一个（顺序相关、偶发红）；
  ② 更糟：真实状态被假槽位覆盖，下次跑真批量时冷却记录全丢，
     而症状是"退避莫名其妙从第一档重来"，根本联想不到是测试干的。

配额台账（`quota.state_path()`）一并隔离，理由同上 —— 那是**用户的真实数据**，
测试不该有机会碰到它。`tests/test_quota.py` 有自己的 `env` 夹具做更细的隔离，
它的 `monkeypatch.setattr(quota, "state_path", ...)` 会覆盖这里的环境变量。
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_runtime_state(tmp_path, monkeypatch):
    """把池子状态与配额台账都指到本用例的 tmp 目录。"""
    monkeypatch.setenv("IR_PROXY_STATE", str(tmp_path / "proxypool.json"))
    monkeypatch.setenv("IR_QUOTA_STATE", str(tmp_path / "register_quota.jsonl"))
