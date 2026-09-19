"""`src/quota.py` 自检 —— 在**临时 state 文件**上跑，不碰真实计数。

为什么要有这个文件
------------------
`src/quota.py` 是"保护"逻辑：它出错的方式很讨厌 —— 不是崩溃，而是
**静默放行**（计数丢了 → 以为还剩 40 个 → 撞墙）。崩溃能被看见，静默放行不能。
所以关键行为必须能一条条验：

  1. 空 state 的基线
  2. 计数累加 + 只记成功（失败不该占配额，这里验的是"record 才计数"）
  3. 严格档 / 宽松档两种 `check_or_raise` 语义
  4. 触顶后的 `exhausted` / `wait_seconds`
  4b. **`used > limit`（补录超额）时的等待时间**（必须按第 N 条滑出算）
  5. **并发 append 不丢行**（多 producer 线程同时写）
  6. 半截 JSON 行被跳过而不是让整个计数归零
  7. 窗口外的旧记录不计入 used
  8. `_compact_if_needed` 真的会重写文件
  9. **出口作用域（`scope`）隔离** —— 槽位池模式下每个出口是独立配额，
     计数必须分开算；且无 `scope` 字段的老记录要能正常解析

跑法：
    python tools/selftest_quota.py
"""

import json
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config, quota  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f"   [{detail}]" if detail else ""))


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="quota-selftest-"))
    state = tmpdir / "register_quota.jsonl"

    # 把 state 重定向到临时文件，并缩小 limit 让触顶容易构造。
    quota.state_path = lambda: state          # type: ignore[assignment]
    config.REG_QUOTA_MAX = 5
    config.REG_QUOTA_WINDOW_H = 6.0

    print(f"\nstate = {state}")
    print(f"limit = {config.REG_QUOTA_MAX}, window = {config.REG_QUOTA_WINDOW_H}h\n")

    # ── 1. 空 state 基线 ──────────────────────────────────────
    print("[1] 空 state 基线")
    st = quota.status()
    check("used == 0", st.used == 0, f"used={st.used}")
    check("remaining == limit", st.remaining == 5, f"remaining={st.remaining}")
    check("not exhausted", not st.exhausted)
    check("wait_seconds == 0（无记录）", st.wait_seconds() == 0.0)

    # ── 2. 计数累加 ───────────────────────────────────────────
    print("\n[2] 计数累加")
    for i in range(3):
        st = quota.record(f"u{i}@x.com")
    check("record 3 次后 used == 3", st.used == 3, f"used={st.used}")
    check("remaining == 2", st.remaining == 2, f"remaining={st.remaining}")
    check("not exhausted", not st.exhausted)
    check("state 文件已生成", state.is_file())
    check("文件行数 == 3", len(state.read_text(encoding="utf-8").splitlines()) == 3)

    # ── 3. check_or_raise 两种语义 ─────────────────────────────
    print("\n[3] check_or_raise 严格档 / 宽松档")
    # used=3, limit=5
    ok = True
    try:
        quota.check_or_raise(planned=2)          # 3+2 == 5，不超
    except quota.QuotaExceeded:
        ok = False
    check("planned=2（刚好用满）不抛", ok)

    raised = False
    try:
        quota.check_or_raise(planned=3)          # 3+3 > 5，严格档该抛
    except quota.QuotaExceeded:
        raised = True
    check("planned=3 严格档抛 QuotaExceeded", raised)

    ok = True
    try:
        st2 = quota.check_or_raise(planned=3, allow_partial=True)
    except quota.QuotaExceeded:
        ok = False
    check("planned=3 宽松档不抛（还有余量）", ok)
    check("宽松档返回的 remaining == 2", st2.remaining == 2, f"remaining={st2.remaining}")

    # ── 4. 触顶 ───────────────────────────────────────────────
    print("\n[4] 触顶后 exhausted / wait_seconds")
    quota.record("u3@x.com")
    st = quota.record("u4@x.com")                # 第 5 个 → 触顶
    check("used == 5 时 exhausted", st.exhausted, f"used={st.used}")
    check("remaining == 0", st.remaining == 0)
    check("wait_seconds > 0（要等窗口滑出）", st.wait_seconds() > 0,
          f"{st.wait_seconds() / 60:.1f} 分钟")
    check("describe() 含'已用尽'", "已用尽" in st.describe(), st.describe())

    raised = False
    try:
        quota.check_or_raise(planned=1, allow_partial=True)
    except quota.QuotaExceeded:
        raised = True
    check("触顶后宽松档也抛（余量为 0）", raised)

    # ── 4b. used > limit（补录超额）时的 wait_seconds ─────────
    # 🔴 这一节是补的 —— 原来的自检只覆盖 `used == limit`，于是漏掉了
    # "补录把计数推过上限"这种真实发生过的状态（实测 53/40）。
    # 那时最早一条滑出只让计数降 1，守卫仍然拦着，必须等到第
    # `used - limit + 1` 条滑出。旧实现只取 oldest_ts，系统性**低估**等待。
    print("\n[4b] used > limit（补录超额）时的 wait_seconds")
    W = config.REG_QUOTA_WINDOW_H * 3600.0
    now = time.time()
    # 7 条记录，第 i 条（从 0 数）距过期还有 100*(i+1) 秒
    lines = []
    for i in range(7):
        lines.append(json.dumps({"ts": now - W + 100.0 * (i + 1),
                                 "email": f"over{i}@x.com"}))
    state.write_text("\n".join(lines) + "\n", encoding="utf-8")

    st = quota.status()
    check("used == 7（超过 limit=5）", st.used == 7, f"used={st.used}")
    check("exhausted", st.exhausted)
    check("must_expire == 3（7-5+1）", st.must_expire == 3,
          f"must_expire={st.must_expire}")
    w = st.wait_seconds()
    check("wait ≈ 300s（第 3 条滑出，不是第 1 条的 100s）",
          290 < w < 310, f"{w:.1f}s")
    check("describe() 标出超额与需滑出条数",
          "超额 2 条" in st.describe() and "需滑出 3 条" in st.describe(),
          st.describe())

    # 刚好等于 limit 时，只需滑出 1 条
    state.write_text("\n".join(lines[:5]) + "\n", encoding="utf-8")
    st = quota.status()
    check("used == limit 时 must_expire == 1", st.must_expire == 1,
          f"must_expire={st.must_expire}")
    check("used == limit 时 wait ≈ 100s", 90 < st.wait_seconds() < 110,
          f"{st.wait_seconds():.1f}s")

    # 未触顶时不该有等待
    state.write_text("\n".join(lines[:3]) + "\n", encoding="utf-8")
    st = quota.status()
    check("used < limit 时 must_expire == 0", st.must_expire == 0)
    check("used < limit 时 wait_seconds == 0", st.wait_seconds() == 0.0)

    # ── 5. 并发 append 不丢行 ─────────────────────────────────
    # 这是**最要紧**的一条：多个 producer 线程各自 append，若用了
    # read-modify-write 就会互相覆盖。JSONL 追加写必须做到零丢失。
    print("\n[5] 并发 append 不丢行（20 线程 × 5 次）")
    state.unlink()                                # 清空重来
    n_threads, per = 20, 5
    barrier = threading.Barrier(n_threads)

    def hammer(tid: int):
        barrier.wait()                            # 尽量让写操作撞在一起
        for j in range(per):
            quota.record(f"t{tid}-{j}@x.com")

    ts = [threading.Thread(target=hammer, args=(i,)) for i in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    lines = [ln for ln in state.read_text(encoding="utf-8").splitlines() if ln.strip()]
    expect = n_threads * per
    check(f"写入 {expect} 行，实际 {len(lines)} 行", len(lines) == expect,
          f"丢了 {expect - len(lines)} 行" if len(lines) != expect else "零丢失")
    st = quota.status()
    check("status().used 与文件行数一致", st.used == expect,
          f"used={st.used}")

    # ── 6. 半截行被跳过 ───────────────────────────────────────
    print("\n[6] 半截 JSON 行（并发写的残骸）被跳过")
    with state.open("a", encoding="utf-8") as f:
        f.write('{"ts": 1.0, "email": "half')     # 故意写半截
        f.write("\n")
        f.write("\n")                             # 空行
        f.write('not json at all\n')
    st = quota.status()
    check("坏行不影响已有计数", st.used == expect, f"used={st.used}")
    check("坏行不被计入", st.used == expect)

    # ── 7. 窗口外的旧记录不计入 ───────────────────────────────
    print("\n[7] 窗口外旧记录不计入 used")
    state.unlink()
    old_ts = time.time() - 7 * 3600               # 7h 前 > 6h 窗口
    with state.open("w", encoding="utf-8") as f:
        for i in range(10):
            f.write(json.dumps({"ts": old_ts, "email": f"old{i}@x.com"}) + "\n")
        f.write(json.dumps({"ts": time.time(), "email": "new@x.com"}) + "\n")
    st = quota.status()
    check("10 条 7h 前的旧记录被排除", st.used == 1, f"used={st.used}")
    check("oldest_ts 是窗口内那条", st.oldest_ts is not None
          and st.oldest_ts > time.time() - 60)
    check("未触顶（旧记录不占额度）", not st.exhausted)

    # ── 8. _compact_if_needed 重写文件 ────────────────────────
    print("\n[8] _compact_if_needed 重写文件（防无限增长）")
    state.unlink()
    with state.open("w", encoding="utf-8") as f:
        for i in range(300):                      # 全部是窗口外的死记录
            f.write(json.dumps({"ts": old_ts, "email": f"dead{i}@x.com"}) + "\n")
    before = len(state.read_text(encoding="utf-8").splitlines())
    quota.record("fresh@x.com")                   # 触发 compact
    after = len(state.read_text(encoding="utf-8").splitlines())
    check(f"300 行死记录被压缩（{before} → {after}）", after <= 2,
          f"after={after}")
    st = quota.status()
    check("压缩后计数仍正确（只剩窗口内 1 条）", st.used == 1, f"used={st.used}")

    # ── 9. OSError 不该让主流程崩 ─────────────────────────────
    print("\n[9] state 不可写时 record 不抛异常（记不上不该拖垮主流程）")
    quota.state_path = lambda: Path(tmpdir) / "no" / "such" / "dir" / "x.jsonl"
    crashed = None
    try:
        quota.record("x@x.com")
    except Exception as ex:                       # noqa: BLE001
        crashed = ex
    # 这里目录能被 mkdir 创建，所以不该崩；关键是**不能抛**
    check("record 未抛异常", crashed is None, repr(crashed) if crashed else "")
    quota.state_path = lambda: state              # 恢复

    # ── 10. 补录历史 ──────────────────────────────────────────
    # 本地计数是"功能上线后"才开始记的。此前注册过的一批若不补录，计数从 0
    # 开始 → 保护形同虚设。补录最容易错的地方有两个：
    #   ① 判据用 status=="success"（整链成功）而不是 register=="ok"
    #      → 漏掉"注册成功但登录失败"的账号，而它们**确实占了配额**
    #   ② 用"补录那一刻"当时间戳 → 两小时前的记录被算成"刚刚发生"
    print("\n[10] backfill 补录历史（含判据 / 时间戳 / 去重 / zip）")
    quota.state_path = lambda: state
    if state.exists():
        state.unlink()

    now = time.time()

    def ca(hours_ago: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(now - hours_ago * 3600))

    hist = [
        {"email": "a@x.com", "created_at": ca(1), "status": "success",
         "stages": {"register": "ok", "login": "ok", "key": "ok"}},
        # 注册成功、登录失败 → 必须补录（占了注册配额）
        {"email": "b@x.com", "created_at": ca(2), "status": "failed",
         "stages": {"register": "ok"}, "error": "login: boom"},
        # 注册就失败 → 不该补录
        {"email": "c@x.com", "created_at": ca(3), "status": "failed",
         "stages": {}, "error": "register: boom"},
        # 窗口外 → 要写入（读取时才按窗口过滤）
        {"email": "d@x.com", "created_at": ca(9), "status": "success",
         "stages": {"register": "ok", "login": "ok", "key": "ok"}},
        # 缺 created_at → 不补录（宁可少记，也不给错时间戳）
        {"email": "e@x.com", "status": "success",
         "stages": {"register": "ok"}},
    ]
    hp = tmpdir / "results.json"
    hp.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")

    added, dup = quota.backfill([str(hp)], dry_run=True)
    check("dry-run 报 3 条待补（a/b/d）", added == 3, f"added={added}")
    check("dry-run 未落盘", not state.exists())

    added, dup = quota.backfill([str(hp)])
    check("实际补录 3 条", added == 3, f"added={added}")
    check("窗口内 used == 2（a + b，d 在窗口外）",
          quota.status().used == 2, f"used={quota.status().used}")

    added, dup = quota.backfill([str(hp)])
    check("重复补录 → 新增 0、重复 3", added == 0 and dup == 3,
          f"added={added} dup={dup}")
    check("重复补录后 used 仍为 2", quota.status().used == 2)

    # zip 支持（历史备份就是 zip）
    zp = tmpdir / "hist.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("tmp/f.json", json.dumps(
            [{"email": "f@x.com", "created_at": ca(1), "status": "success",
              "stages": {"register": "ok"}}], ensure_ascii=False))
        z.writestr("readme.txt", "not json")
    added, dup = quota.backfill([str(zp)])
    check("zip 内的 json 被补录（+1）", added == 1, f"added={added}")
    check("补录后 used == 3", quota.status().used == 3,
          f"used={quota.status().used}")

    added, dup = quota.backfill([str(tmpdir / "nope.json")])
    check("不存在的文件不崩、不计数", added == 0, f"added={added}")

    # ── 9. 出口作用域（scope）隔离 ────────────────────────────
    # 槽位池模式下每个出口是**独立配额**，计数必须按出口分开。
    # 这里用干净 state 重跑，避免前面 8 组用例的污染。
    print("\n[9] 出口作用域（scope）隔离")
    state2 = tmpdir / "scope_quota.jsonl"
    quota.state_path = lambda: state2          # type: ignore[assignment]
    config.REG_QUOTA_MAX = 2

    # 9a. 无 scope 的历史记录属于"老出口"，不该被新槽位看到
    state2.write_text(
        json.dumps({"ts": time.time(), "email": "old@x.com"}) + "\n"
        + json.dumps({"ts": time.time(), "email": "old2@x.com"}) + "\n",
        encoding="utf-8")
    check("全局 status() 看到老记录（used=2）", quota.status().used == 2,
          f"used={quota.status().used}")
    check("scope='slot1' 看不到老记录（used=0）",
          quota.status(scope="slot1").used == 0,
          f"used={quota.status(scope='slot1').used}")
    check("老记录全在 scope='' 里", quota.status(scope="").used == 2,
          f"used={quota.status(scope='').used}")

    # 9b. 各 scope 独立累加、互不影响
    quota.record("a@x.com", scope="slot1")
    quota.record("b@x.com", scope="slot2")
    quota.record("c@x.com", scope="slot2")
    check("slot1 used == 1", quota.status(scope="slot1").used == 1,
          f"used={quota.status(scope='slot1').used}")
    check("slot2 used == 2", quota.status(scope="slot2").used == 2,
          f"used={quota.status(scope='slot2').used}")
    check("slot3 used == 0（没记过）", quota.status(scope="slot3").used == 0,
          f"used={quota.status(scope='slot3').used}")
    check("全局 used == 5（2 老 + 3 新，跨 scope 求和）", quota.status().used == 5,
          f"used={quota.status().used}")
    check("record 返回值就是该 scope 的状态（不是全局）",
          quota.record("d@x.com", scope="slot1").used == 2,
          "slot1 记第 2 条后应为 2")

    # 9c. 一个出口触顶不影响别的出口 —— 这是整个 scope 机制的目的
    check("slot1 触顶（2/2）", quota.status(scope="slot1").exhausted)
    check("slot2 同样触顶（2/2）", quota.status(scope="slot2").exhausted)
    check("slot3 仍然空（0/2）—— 不被 slot1/2 拖累",
          not quota.status(scope="slot3").exhausted)
    ok = True
    try:
        quota.check_or_raise(planned=1, scope="slot1")
        ok = False
    except quota.QuotaExceeded:
        pass
    check("check_or_raise(scope='slot1') 抛（该出口满了）", ok)
    ok = True
    try:
        quota.check_or_raise(planned=1, scope="slot3")
    except quota.QuotaExceeded:
        ok = False
    check("check_or_raise(scope='slot3') 不抛（该出口还有余量）", ok)

    # 9d. 向后兼容：老文件里的行没有 scope 字段，解析不能崩
    check("老格式行（无 scope 字段）被正常解析", quota.status(scope="").used == 2,
          f"used={quota.status(scope='').used}")

    # ── 汇总 ─────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for n in FAIL:
            print(f"  ✗ {n}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
