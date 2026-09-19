"""`src/ledger.py` 的合并逻辑与防缩水护栏 —— 离线，零网络请求。

从 `tools/selftests/selftest_merge.py` **保真迁移**：输入数据、期望值、断言条件一律未改，
只把自定义的 `check(name, cond, detail)` 换成 `assert cond, detail`。
（源文件已于 2026-09-19 移除 —— 本文件是**唯一真源**。）

为什么值得单独钉住
------------------
`results.json` 同时是"运行报告"和"账号台账"。2026-09-18 实测：一次
`--count 1` 的探测就把 53 条台账覆盖成了 1 条 —— **静默缩水，没有任何报错**。
台账已经丢过两次，所以这里宁可多写测试。

跑法：
    pytest tests/test_ledger_merge.py -v
"""

import json
import pathlib

import pytest

from src import ledger

ROOT = pathlib.Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results.json"


@pytest.fixture(scope="module")
def real():
    """真实台账。

    ⚠ 基线必须取**实际条数**，不能硬编码：台账是活的（每跑一次批量注册就变多），
      写死 53 的话下次正常注册就会被判"测试失败" —— 那是测试在撒谎，不是代码坏了。
    """
    return ledger.load_existing(RESULTS)


@pytest.fixture
def old_csv():
    """T9 用的"CSV 来源"老记录。每次新建，避免被上一轮合并就地改写。"""
    return {"email": "c@x.com", "username": "u", "password": "p",
            "api_key": "sk-old", "credits": "10.000000"}


@pytest.fixture
def new_ds():
    """T9 用的"下游写回"新记录 —— 刻意**没有 status**（rank 恒为 0）。"""
    return {"email": "c@x.com", "jwt": "eyJ...", "credits": "6.547000",
            "verify": "ok(10 models)", "downstream": "ok"}


# ── T1 ────────────────────────────────────────────────────────────────
def test_t1_new_failure_does_not_drop_history(real):
    base = len(real)
    new = [{"email": "oops-new@x.com", "status": "failed", "stages": {}}]
    m, kept, _added, up = ledger.merge_records(real, new)
    assert kept == base, f"kept={kept}"
    assert len(m) == base + 1, f"len={len(m)}"
    assert up == 0, f"upgraded={up}"


# ── T2 ────────────────────────────────────────────────────────────────
def test_t2_existing_success_not_downgraded_by_failure(real):
    tgt = next(r for r in real if r.get("status") == "success")
    m2, _k, _a, _u = ledger.merge_records(
        real, [{"email": tgt["email"], "status": "failed", "stages": {}}])
    after = next(r for r in m2 if r["email"] == tgt["email"])
    assert after.get("status") == "success", after.get("status")
    assert len(m2) == len(real), f"len={len(m2)}"


# ── T3 ────────────────────────────────────────────────────────────────
def test_t3_success_upgrades_existing_failure():
    m3, _k, _a, up3 = ledger.merge_records(
        [{"email": "u@x.com", "status": "failed", "stages": {}}],
        [{"email": "u@x.com", "status": "success", "api_key": "sk-1",
          "stages": {"register": "ok"}}])
    assert m3[0]["status"] == "success" and up3 == 1, \
        f"status={m3[0]['status']} upgraded={up3}"
    assert len(m3) == 1, f"len={len(m3)}"


# ── T4 ────────────────────────────────────────────────────────────────
def test_t4_duplicate_email_in_old_file_keeps_first():
    m4, _k, _a, _u = ledger.merge_records(
        [{"email": "d@x.com", "status": "success", "n": 1},
         {"email": "d@x.com", "status": "failed", "n": 2}], [])
    assert len(m4) == 1 and m4[0]["n"] == 1, f"len={len(m4)}"


# ── T5 ────────────────────────────────────────────────────────────────
def test_t5_records_without_email_kept_verbatim():
    m5, _k, _a, _u = ledger.merge_records(
        [{"status": "failed"}, {"status": "failed"}], [])
    assert len(m5) == 2, f"len={len(m5)}"


# ── T6 ────────────────────────────────────────────────────────────────
def test_t6_load_existing_survives_corrupt_missing_and_nonlist(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert ledger.load_existing(bad) == []
    assert ledger.load_existing(tmp_path / "nope.json") == []
    obj = tmp_path / "obj.json"
    obj.write_text('{"a":1}', encoding="utf-8")
    assert ledger.load_existing(obj) == []


# ── T7 ────────────────────────────────────────────────────────────────
def test_t7_save_guard_against_silent_shrinkage(real, tmp_path):
    p = tmp_path / "results.json"
    p.write_text(json.dumps(real, ensure_ascii=False), encoding="utf-8")

    # 条数变少 -> 必须抛，且**文件不能被改动**（抛之前就写盘等于没护栏）
    with pytest.raises(ValueError):
        ledger.save(p, real[:10])
    assert len(ledger.load_existing(p)) == len(real), \
        f"len={len(ledger.load_existing(p))}"

    # 条数变多 -> 放行
    ledger.save(p, real + [{"email": "n@x.com", "status": "failed"}])
    assert len(ledger.load_existing(p)) == len(real) + 1

    # --overwrite 路径（existing=[]）-> 放行
    ledger.save(p, [{"email": "only@x.com", "status": "failed"}], existing=[])
    assert len(ledger.load_existing(p)) == 1


# ── T8 ────────────────────────────────────────────────────────────────
def test_t8_end_to_end_real_ledger_plus_one_failure(real, tmp_path):
    base = len(real)
    new = [{"email": "oops-new@x.com", "status": "failed", "stages": {}}]
    out = tmp_path / "results.json"
    out.write_text(json.dumps(real, ensure_ascii=False), encoding="utf-8")

    existing = ledger.load_existing(out)
    merged, _k, _a, _u = ledger.merge_records(existing, new)
    assert len(existing) == base, f"len={len(existing)}"
    assert len(merged) == base + 1, f"len={len(merged)}"

    ledger.save(out, merged)
    assert len(ledger.load_existing(out)) == base + 1


# ── T9 ────────────────────────────────────────────────────────────────
# 🔴 这组规则是 2026-09-18 才补的：`tools/run_downstream.py` 交回的是
#    **增量字段**（jwt / credits / verify / timings_downstream），
#    **没有 status** → rank 恒为 0。若只比 rank，则 `0 > 0` 为假，
#    下游跑完一个字段都写不进去，而打印全"正常" —— 静默缩水。
def test_t9a_rank_tie_merges_union_of_fields(old_csv, new_ds):
    m9, _k, _a, up9 = ledger.merge_records([old_csv], [new_ds])
    assert ledger.rank(old_csv) == ledger.rank(new_ds) == 0
    assert up9 == 1, f"upgraded={up9}"
    assert m9[0].get("jwt") == "eyJ..." and m9[0].get("verify") == "ok(10 models)", \
        f"keys={sorted(m9[0].keys())}"
    assert m9[0].get("username") == "u" and m9[0].get("api_key") == "sk-old", \
        f"username={m9[0].get('username')} api_key={m9[0].get('api_key')}"
    assert m9[0].get("credits") == "6.547000", m9[0].get("credits")
    assert len(m9) == 1, f"len={len(m9)}"


def test_t9b_fewer_fields_does_not_change_old_record(old_csv):
    """反向：新记录字段更少 -> 老字段一个都不能少。"""
    m9b, _k, _a, up9b = ledger.merge_records([old_csv], [{"email": "c@x.com"}])
    assert up9b == 0 and m9b[0].get("api_key") == "sk-old", f"upgraded={up9b}"


def test_t9c_rank_beats_field_count():
    """rank 优先于字段数：失败记录字段再多也不能盖掉成功记录。"""
    m9c, _k, _a, up9c = ledger.merge_records(
        [{"email": "s@x.com", "status": "success", "api_key": "sk-1"}],
        [{"email": "s@x.com", "status": "failed", "a": 1, "b": 2, "c": 3, "d": 4}])
    assert up9c == 0 and m9c[0]["status"] == "success", f"upgraded={up9c}"


def test_t9d_success_rerun_keeps_old_fields():
    """同级都是 success：并集也要生效（重跑一次下游不该丢上次的字段）。"""
    m9d, _k, _a, up9d = ledger.merge_records(
        [{"email": "r@x.com", "status": "success", "api_key": "sk-1",
          "jwt": "old", "timings": {"login": 999}}],
        [{"email": "r@x.com", "status": "success", "api_key": "sk-1",
          "verify": "ok(10 models)"}])
    assert up9d == 1 and m9d[0].get("jwt") == "old" \
        and m9d[0].get("verify") == "ok(10 models)", \
        f"upgraded={up9d} keys={sorted(m9d[0].keys())}"
