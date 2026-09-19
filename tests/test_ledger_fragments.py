"""`src/ledger.merge_fragments()` —— 重建台账时的"碎片合并"规则。

为什么单独钉住
--------------
`tools/data/restore_results.py` 原来自带 `_better()`（先比 rank、再比**非空字段数**）
+ `_fill_missing()`。2026-09-19 落地重构方案 #10 时想把它换成
`ledger.merge_records()`（看起来是重复实现），实测**被数据否决**：

    旧算法（richness 启发式）  217 账号 / 3111 字段 / 0 个账号未达理论最大
    ledger.merge_records      217 账号 / 3093 字段 / **15 个账号丢 18 个字段**

根因是两个函数的**降级分支**不同，而且这个不同**必须保留**（理由见
`merge_fragments` 的 docstring）。所以最终落地为：把规则抽成
`merge_fragments`（住在 `ledger` 里，单一真源），`restore_results` 调它，
`merge_records` **一行没改**。

这些用例钉住的是"字段只增不减"这条**性质**，不是某个具体输出值 ——
具体输出值另有一条字节级回归（见文件末尾）。

跑法：
    pytest tests/test_ledger_fragments.py -v
"""

import pathlib

import pytest

from src import ledger

ROOT = pathlib.Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results.json"


@pytest.fixture(scope="module")
def real():
    return ledger.load_existing(RESULTS)


# 真实形状的碎片。`SUCCESS` 是流水线结果（rank=2），`EXPORT` 是
# `export_keys` 的导出行（rank=0，没有 `status`）—— 后者带 source/verify。
SUCCESS = {"email": "a@x.com", "status": "success", "username": "u",
           "password": "p", "api_key": "sk-1", "jwt": "eyJ...",
           "sso_uid": "42", "stages": {"register": "ok"}}
EXPORT = {"email": "a@x.com", "created_at": "2026-09-18 12:00:00",
          "username": "u", "password": "p", "api_key": "sk-1",
          "key_id": "k1", "credits": "10.000000",
          "verify": "ok(10 models)", "source": "opt6_batch50"}


# ── 回归：这就是 #10 当初丢掉的 18 个字段 ────────────────────────────
def test_export_fragment_fills_gaps_on_success_record():
    """rank=0 的导出行里的 `source` / `verify` 必须补进 rank=2 的记录。

    `merge_records` 在这里什么都不做（降级门控），于是 15 个账号的 `source`
    和 3 个的 `verify` 全丢 —— 它们本来都够得着理论最大字段集。
    """
    m = ledger.merge_fragments([SUCCESS, EXPORT])
    assert m["status"] == "success", m["status"]
    assert m["source"] == "opt6_batch50", m.get("source")
    assert m["verify"] == "ok(10 models)", m.get("verify")
    assert m["credits"] == "10.000000", m.get("credits")
    assert m["key_id"] == "k1", m.get("key_id")
    # 高 rank 碎片的独有字段一个都不能少
    assert m["jwt"] == "eyJ..." and m["sso_uid"] == "42"
    assert m["stages"] == {"register": "ok"}


def test_contrast_with_merge_records_downgrade():
    """⚠ 这是**有意**的差异，不是漏改 —— 谁把两者"统一"了，这条会红。

    `merge_records` 的降级分支必须什么都不做：运行期那条 rank=0 记录往往是
    一次**失败尝试**，它的 `error` 不该挂到一个已经成功的账号上
    （`tools/run_downstream.py:269` 正是靠"并集 + 显式写空"清陈旧 `error`）。
    """
    frag = dict(EXPORT, error="B0000 quota blocked")
    by_records, *_ = ledger.merge_records([SUCCESS], [frag])
    assert "source" not in by_records[0], by_records[0].keys()
    assert "error" not in by_records[0], by_records[0].keys()

    by_fragments = ledger.merge_fragments([SUCCESS, frag])
    assert by_fragments["source"] == "opt6_batch50"


# ── 性质：字段只增不减 ────────────────────────────────────────────────
@pytest.mark.parametrize("frags", [
    [SUCCESS],
    [SUCCESS, EXPORT],
    [EXPORT, SUCCESS],
    [SUCCESS, {"email": "a@x.com"}, {"email": "a@x.com", "jwt": ""}],
    [{"email": "a@x.com", "status": "failed"}, SUCCESS, EXPORT],
    [{"email": "a@x.com", "status": "skipped"}, EXPORT, SUCCESS],
])
def test_key_set_equals_union_of_all_fragments(frags):
    """结果的键集合 == 所有碎片键的并集。这是"不丢字段"的充要形式。"""
    m = ledger.merge_fragments(frags)
    union = set().union(*(set(f) for f in frags))
    assert set(m) == union, f"丢 {sorted(union - set(m))} / 多 {sorted(set(m) - union)}"


def test_empty_fragment_list_returns_empty_dict():
    assert ledger.merge_fragments([]) == {}


# ── 取值优先级 ────────────────────────────────────────────────────────
def test_higher_rank_fragment_may_overwrite():
    """失败 -> 成功：rank 更高的碎片可以覆盖值。"""
    m = ledger.merge_fragments(
        [{"email": "a@x.com", "status": "failed", "error": "B0000"},
         {"email": "a@x.com", "status": "success", "api_key": "sk-1"}])
    assert m["status"] == "success" and m["api_key"] == "sk-1"
    # 低 rank 碎片**独有**的键仍然保留（只是不覆盖）
    assert m["error"] == "B0000"


def test_lower_rank_fragment_cannot_overwrite_nonempty():
    m = ledger.merge_fragments(
        [SUCCESS, {"email": "a@x.com", "status": "failed", "jwt": "坏值"}])
    assert m["jwt"] == "eyJ...", m["jwt"]
    assert m["status"] == "success"


def test_first_source_wins_on_rank_tie():
    """同级碎片：靠前的来源胜出（来源列表本身就是优先级列表）。"""
    a = {"email": "a@x.com", "credits": "10.000000", "jwt": "新"}
    b = {"email": "a@x.com", "credits": "9.000000", "jwt": "旧"}
    assert ledger.merge_fragments([a, b])["jwt"] == "新"
    # b 独有的键照样补进来
    assert ledger.merge_fragments([a, {"email": "a@x.com", "verify": "ok"}])["verify"] == "ok"


def test_empty_value_never_clobbers_nonempty():
    """空值不覆盖非空值 —— 哪怕它来自更高 rank 的碎片。"""
    m = ledger.merge_fragments(
        [{"email": "a@x.com", "status": "success", "jwt": "eyJ..."},
         {"email": "a@x.com", "status": "success", "jwt": ""}])
    assert m["jwt"] == "eyJ...", m["jwt"]


def test_zero_and_false_are_real_values():
    """`0` / `False` **不是**空值：余额 0、"验证不通过"都是有效观测。

    把它们当空值会出两种错：高优先级的 0 盖不住旧值，或者 0 干脆补不进去。
    """
    # 判据一：来源靠前的 0 能盖掉靠后的非零值（它是真值，不是"没填"）
    m = ledger.merge_fragments(
        [{"email": "a@x.com", "credits": 0, "verify": False},
         {"email": "a@x.com", "credits": "5.000000", "verify": "ok"}])
    assert m["credits"] == 0, m["credits"]
    assert m["verify"] is False, m["verify"]

    # 判据二：只有 0 / False 的碎片能把它们补进缺这个键的记录
    m2 = ledger.merge_fragments(
        [{"email": "a@x.com", "status": "success"},
         {"email": "a@x.com", "credits": 0, "verify": False}])
    assert m2["credits"] == 0, m2["credits"]
    assert m2["verify"] is False, m2["verify"]


def test_single_fragment_is_returned_verbatim():
    """单碎片原样返回：值相等，且**键顺序**不变。"""
    m = ledger.merge_fragments([SUCCESS])
    assert m == SUCCESS
    assert list(m) == list(SUCCESS)


def test_key_order_follows_source_priority():
    """键顺序跟来源优先级一致 —— `results.json` 的字段在前，补上的在后。

    `results.json` 是人会直接看的台账，键顺序乱读起来费劲。
    """
    m = ledger.merge_fragments([SUCCESS, EXPORT])
    assert list(m)[:len(SUCCESS)] == list(SUCCESS)
    assert list(m)[len(SUCCESS):] == [k for k in EXPORT if k not in SUCCESS]


def test_real_ledger_records_survive_verbatim(real):
    """真实台账每一条单独过一遍都必须原样返回（不许被"顺手整理"）。"""
    assert real, "读不到 results.json"
    for rec in real:
        assert ledger.merge_fragments([rec]) == rec, rec.get("email")


def test_real_ledger_single_source_is_idempotent(real):
    """重建是幂等的：把已合并的台账再当唯一来源过一遍，结果不变。

    这一条防的是"每跑一次重建就悄悄改动几个字段" —— 那种漂移最难发现。
    """
    assert real, "读不到 results.json"
    again = [ledger.merge_fragments([r]) for r in real]
    assert again == real
