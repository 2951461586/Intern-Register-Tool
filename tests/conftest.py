"""pytest 全局夹具 —— 运行态文件隔离 + 台账来源。

一、运行态文件重定向（`_isolate_runtime_state`，autouse）
------------------------------------------------------
`ProxySlotPool` 一构造就读状态文件（`_load_state`），一 `report_banned` /
`report_failed` 就写状态文件（`_save_state_locked`）。测试里构造的池子用的是
**假槽位**（`127.0.0.1:7901` 之类），放任它读写真实路径会有两种坏结果：

  ① 测试之间互相污染 —— 上一个用例留下的冷却会影响下一个（顺序相关、偶发红）；
  ② 更糟：真实状态被假槽位覆盖，下次跑真批量时冷却记录全丢，
     而症状是"退避莫名其妙从第一档重来"，根本联想不到是测试干的。

配额台账（`quota.state_path()`）一并隔离，理由同上 —— 那是**用户的真实数据**，
测试不该有机会碰到它。`tests/test_quota.py` 有自己的 `env` 夹具做更细的隔离，
它的 `monkeypatch.setattr(quota, "state_path", ...)` 会覆盖这里的环境变量。

二、台账来源：样本 + 真实（`ledger_sample` / `real_ledger` / `any_ledger`）
----------------------------------------------------------------------------
🔴 2026-09-19 CI 连续红了三批，第二批的根因就在这里。

`results.json` 同时是"运行报告"和"账号台账"，但它**含可直接登录的账号与
API Key**，所以被 `.gitignore` 排除、**不在仓库里** ⇒ CI 上 `load_existing()`
返回 `[]`。而当时两个测试文件各自定义了一个 `real` 夹具把它读进来，
于是同一个根因在 CI 上炸出**三种完全不同的形态**：

    AssertionError: 读不到 results.json     ← 显式断言
    StopIteration                           ← next() 找不到 status=success 的记录
    Failed: DID NOT RAISE ValueError        ← save([]) vs [] 不算缩水
    （外加两条**假绿**：len([]) == 0，断言 0 + 1 == 1 碰巧成立）

**空输入是最坏的一种降级** —— 它既不报错也不跳过，而是让每个用例以各自的
方式"看起来跑过了"。所以现在的规矩是：

  * `ledger_sample` —— 仓库内的**脱敏样本**（`tests/fixtures/ledger_sample.json`），
    形状逐档复刻真实台账（14 / 33 / 34 键的成功记录、空 email 的配额拦截记录、
    9 键的 `export_keys` 导出行、`0` / `False` / `None` / `""` / `{}` / `[]` /
    非 ASCII 值）。**任何环境都可用**，CI 上靠它跑。
  * `real_ledger` —— 真实台账。读不到就**大声跳过**，绝不返回 `[]`。
  * `any_ledger` —— 两者**都跑**。

⚠ 刻意不做成"有真数据就用真数据、没有就用样本"：那样本地测的输入和 CI 测的
  输入**不是同一个**，本地绿就证明不了 CI 绿 —— 而那正是这批失败的根本形态。
"""

from pathlib import Path

import pytest

from src import ledger

ROOT = Path(__file__).resolve().parents[1]

# 仓库内的脱敏样本台账 —— 形状与真实 results.json 一致，值全是编造的。
# 它的形状覆盖由 `tests/test_ledger_sample.py` 钉住（改样本改坏了会红）。
SAMPLE_LEDGER = ROOT / "tests" / "fixtures" / "ledger_sample.json"

# 真实台账。含明文账号 / 密码 / JWT / API Key ⇒ 被 .gitignore 排除 ⇒ CI 上不存在。
REAL_LEDGER = ROOT / "results.json"


@pytest.fixture(autouse=True)
def _isolate_runtime_state(tmp_path, monkeypatch):
    """把池子状态与配额台账都指到本用例的 tmp 目录。"""
    monkeypatch.setenv("IR_PROXY_STATE", str(tmp_path / "proxypool.json"))
    monkeypatch.setenv("IR_QUOTA_STATE", str(tmp_path / "register_quota.jsonl"))


@pytest.fixture(scope="session")
def ledger_sample():
    """脱敏样本台账 —— **任何环境都可用**，CI 上唯一可用的台账来源。"""
    recs = ledger.load_existing(SAMPLE_LEDGER)
    assert recs, f"样本台账读不出来：{SAMPLE_LEDGER}（文件被删了？）"
    return recs


@pytest.fixture
def real_ledger():
    """真实台账 —— 只在本地存在。

    ⚠ 读不到时**必须 `pytest.skip`，不能返回 `[]`**：空列表会让用例以
      `StopIteration` / `DID NOT RAISE` / 静默假绿等各不相同的形态"跑过去"，
      排查成本极高（2026-09-19 实测）。空输入要**大声跳过**。

    刻意用函数级作用域（不用 session）：session 级夹具抛 `Skipped` 会被缓存后
    重抛，行为随 pytest 版本有差异；这里每次只是一次本地文件读取，不值得赌。
    """
    recs = ledger.load_existing(REAL_LEDGER)
    if not recs:
        pytest.skip(
            f"本机没有 {REAL_LEDGER.name}（含凭据、不入库），跳过真实台账用例。"
            "样本台账用例（ledger_sample）在任何环境都会跑。"
        )
    return recs


@pytest.fixture(params=["ledger_sample", "real_ledger"])
def any_ledger(request):
    """台账**参数化**：样本与真实台账各跑一遍。

    本地跑两路（样本 + 真实），CI 上真实那一路显式跳过 —— 于是本地覆盖最广，
    而 CI 的输入始终是本地的**子集**，不会出现"本地测的东西 CI 根本没测"。
    """
    return request.getfixturevalue(request.param)
