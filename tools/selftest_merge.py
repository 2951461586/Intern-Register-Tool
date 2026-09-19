"""离线验证 `src/ledger.py` 的合并逻辑与防缩水护栏（不发任何网络请求）。

背景：`results.json` 同时是"运行报告"和"账号台账"，一次 `--count 1` 探测
就把 53 条台账覆盖成了 1 条（2026-09-18 实测）。这里把修复后的规则逐条钉住。
"""

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src import ledger  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"   [{detail}]" if detail else ""))


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    real = ledger.load_existing(root / "results.json")
    # 🔴 基线必须取**实际条数**，不能硬编码。
    #    台账是活的（每跑一次批量注册就变多），写死 53 的话下次正常注册
    #    就会被判"自测失败" —— 那是自测在撒谎，不是代码坏了。
    base = len(real)
    print(f"现有台账 {base} 条\n")

    print("[T1] 本次 1 条失败、新邮箱 -> 不该丢历史")
    new = [{"email": "oops-new@x.com", "status": "failed", "stages": {}}]
    m, kept, added, up = ledger.merge_records(real, new)
    check(f"原有 {base} 条全部保留", kept == base, f"kept={kept}")
    check(f"新增 1 条 -> 共 {base + 1}", len(m) == base + 1, f"len={len(m)}")
    check("没有记录被覆盖", up == 0, f"upgraded={up}")

    print("\n[T2] 本次失败、但邮箱是已存在的成功账号 -> 不该把成功标成失败")
    tgt = next(r for r in real if r.get("status") == "success")
    m2, _k, _a, _u = ledger.merge_records(real, [
        {"email": tgt["email"], "status": "failed", "stages": {}}])
    after = next(r for r in m2 if r["email"] == tgt["email"])
    check("该账号仍是 success", after.get("status") == "success", after.get("status"))
    check("总条数不变", len(m2) == len(real), f"len={len(m2)}")

    print("\n[T3] 本次成功、邮箱已存在为失败 -> 应升级覆盖")
    m3, _k, _a, up3 = ledger.merge_records(
        [{"email": "u@x.com", "status": "failed", "stages": {}}],
        [{"email": "u@x.com", "status": "success", "api_key": "sk-1",
          "stages": {"register": "ok"}}])
    check("失败记录被成功覆盖", m3[0]["status"] == "success" and up3 == 1,
          f"status={m3[0]['status']} upgraded={up3}")
    check("未新增重复条目", len(m3) == 1, f"len={len(m3)}")

    print("\n[T4] 同一 email 在旧文件里重复 -> 只留第一条")
    m4, _k, _a, _u = ledger.merge_records(
        [{"email": "d@x.com", "status": "success", "n": 1},
         {"email": "d@x.com", "status": "failed", "n": 2}], [])
    check("去重后 1 条", len(m4) == 1 and m4[0]["n"] == 1, f"len={len(m4)}")

    print("\n[T5] 无 email 的记录原样保留（不参与去重）")
    m5, _k, _a, _u = ledger.merge_records(
        [{"status": "failed"}, {"status": "failed"}], [])
    check("两条都保留", len(m5) == 2, f"len={len(m5)}")

    print("\n[T6] load_existing 对损坏/缺失/非 list 不崩")
    with tempfile.TemporaryDirectory() as td:
        bad = pathlib.Path(td) / "bad.json"
        bad.write_text("{ not json", encoding="utf-8")
        check("损坏 JSON -> 0 条", ledger.load_existing(bad) == [])
        check("不存在 -> 0 条", ledger.load_existing(pathlib.Path(td) / "nope.json") == [])
        obj = pathlib.Path(td) / "obj.json"
        obj.write_text('{"a":1}', encoding="utf-8")
        check("非 list -> 0 条", ledger.load_existing(obj) == [])

    print("\n[T7] save 的防静默缩水护栏")
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "results.json"
        p.write_text(json.dumps(real, ensure_ascii=False), encoding="utf-8")
        raised = False
        try:
            ledger.save(p, real[:10])
        except ValueError:
            raised = True
        check("条数变少 -> 抛 ValueError", raised)
        check("抛异常后文件未被改动", len(ledger.load_existing(p)) == len(real),
              f"len={len(ledger.load_existing(p))}")
        ok = True
        try:
            ledger.save(p, real + [{"email": "n@x.com", "status": "failed"}])
        except ValueError:
            ok = False
        check("条数变多 -> 放行", ok and len(ledger.load_existing(p)) == len(real) + 1)
        ok2 = True
        try:
            ledger.save(p, [{"email": "only@x.com", "status": "failed"}], existing=[])
        except ValueError:
            ok2 = False
        check("--overwrite 路径（existing=[]）-> 放行",
              ok2 and len(ledger.load_existing(p)) == 1)

    print("\n[T8] 端到端：真实台账 + 一次 1 条失败的真实场景")
    with tempfile.TemporaryDirectory() as td:
        out = pathlib.Path(td) / "results.json"
        out.write_text(json.dumps(real, ensure_ascii=False), encoding="utf-8")
        existing = ledger.load_existing(out)
        merged, kept, added, _u = ledger.merge_records(existing, new)
        check(f"读回 {base} 条", len(existing) == base, f"len={len(existing)}")
        check(f"合并后 {base + 1} 条（不是 1 条）", len(merged) == base + 1,
              f"len={len(merged)}")
        ok = True
        try:
            ledger.save(out, merged)
        except ValueError:
            ok = False
        check("写盘通过护栏", ok and len(ledger.load_existing(out)) == base + 1)

    print("\n[T9] rank 打平时取**并集**（下游增量写回的回归测试）")
    # 🔴 这条规则是 2026-09-18 才补的：`tools/run_downstream.py` 交回的是
    #    **增量字段**（jwt / credits / verify / timings_downstream），
    #    **没有 status** → rank 恒为 0。若只比 rank，则 `0 > 0` 为假，
    #    下游跑完一个字段都写不进去，而打印全"正常" —— 静默缩水。
    old_csv = {"email": "c@x.com", "username": "u", "password": "p",
               "api_key": "sk-old", "credits": "10.000000"}
    new_ds = {"email": "c@x.com", "jwt": "eyJ...", "credits": "6.547000",
              "verify": "ok(10 models)", "downstream": "ok"}
    m9, _k, _a, up9 = ledger.merge_records([old_csv], [new_ds])
    check("无 status 双方 rank 均为 0", ledger.rank(old_csv) == ledger.rank(new_ds) == 0)
    check("同级 -> 合并生效", up9 == 1, f"upgraded={up9}")
    check("下游新字段真的落进台账",
          m9[0].get("jwt") == "eyJ..." and m9[0].get("verify") == "ok(10 models)",
          f"keys={sorted(m9[0].keys())}")
    check("旧记录独有字段没被冲掉",
          m9[0].get("username") == "u" and m9[0].get("api_key") == "sk-old",
          f"username={m9[0].get('username')} api_key={m9[0].get('api_key')}")
    check("冲突字段以新值为准", m9[0].get("credits") == "6.547000",
          m9[0].get("credits"))
    check("条数不变（没有新增重复）", len(m9) == 1, f"len={len(m9)}")

    # 反向：新记录字段更少 -> 老字段一个都不能少
    m9b, _k, _a, up9b = ledger.merge_records([old_csv], [{"email": "c@x.com"}])
    check("字段更少 -> 不改变老记录", up9b == 0 and m9b[0].get("api_key") == "sk-old",
          f"upgraded={up9b}")

    # rank 优先于字段数：失败记录字段再多也不能盖掉成功记录
    m9c, _k, _a, up9c = ledger.merge_records(
        [{"email": "s@x.com", "status": "success", "api_key": "sk-1"}],
        [{"email": "s@x.com", "status": "failed", "a": 1, "b": 2, "c": 3, "d": 4}])
    check("rank 低但字段多 -> 仍不覆盖（rank 优先）",
          up9c == 0 and m9c[0]["status"] == "success", f"upgraded={up9c}")

    # 同级都是 success：并集也要生效（重跑一次下游不该丢上次的字段）
    m9d, _k, _a, up9d = ledger.merge_records(
        [{"email": "r@x.com", "status": "success", "api_key": "sk-1",
          "jwt": "old", "timings": {"login": 999}}],
        [{"email": "r@x.com", "status": "success", "api_key": "sk-1",
          "verify": "ok(10 models)"}])
    check("success 重跑 -> 旧 jwt 保留、新 verify 落盘",
          up9d == 1 and m9d[0].get("jwt") == "old"
          and m9d[0].get("verify") == "ok(10 models)",
          f"upgraded={up9d} keys={sorted(m9d[0].keys())}")

    print(f"\n{'=' * 56}\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败：")
        for n in FAIL:
            print("  ✗", n)
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
