"""`src/quota.py` 的行为测试 —— 在**临时 state 文件**上跑，不碰真实计数。

从 `tools/selftests/selftest_quota.py` **保真迁移**：输入数据、期望值、断言条件一律未改，
只把 `check(name, cond, detail)` 换成 `assert cond, detail`，并补了 pytest 的
`monkeypatch` 隔离（原脚本直接改全局，跑完不恢复）。
（源文件已于 2026-09-19 移除 —— 本文件是**唯一真源**。）

为什么值得单独钉住
------------------
`src/quota.py` 是"保护"逻辑，它出错的方式很讨厌 —— 不是崩溃，而是
**静默放行**（计数丢了 → 以为还剩 40 个 → 撞墙）。崩溃能被看见，静默放行不能。
所以关键行为必须能一条条验。

⚠ 分组说明：原脚本的 `[1]`–`[4]` 是**一条状态链**（空 → 记 3 条 → 判额度 →
触顶），`[5]`–`[6]` 也是一条（并发写 100 行 → 追加坏行）。拆成独立函数会
要求重复构造前置状态，反而增加失真风险，所以各自合并为一个函数。

跑法：
    pytest tests/test_quota.py -v
"""

import json
import threading
import time
import zipfile

import pytest

from src import config, quota


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把 state 重定向到临时文件，并缩小 limit 让触顶容易构造。

    原脚本用的是 `tempfile.mkdtemp()` + 直接赋值全局；这里换成 pytest 的
    `tmp_path` + `monkeypatch`，跑完自动还原，不会污染其它测试。
    """
    state = tmp_path / "register_quota.jsonl"
    monkeypatch.setattr(quota, "state_path", lambda: state)
    monkeypatch.setattr(config, "REG_QUOTA_MAX", 5)
    monkeypatch.setattr(config, "REG_QUOTA_WINDOW_H", 6.0)
    return state


@pytest.fixture
def scope_env(tmp_path, monkeypatch):
    """出口作用域分组专用：独立 state 文件 + limit=2。"""
    state2 = tmp_path / "scope_quota.jsonl"
    monkeypatch.setattr(quota, "state_path", lambda: state2)
    monkeypatch.setattr(config, "REG_QUOTA_MAX", 2)
    return state2


# ── [1]–[4] 一条状态链：空 → 记 3 条 → 判额度 → 触顶 ────────────────────
def test_01_to_04_baseline_accumulate_and_exhaust(env):
    state = env

    # ── 1. 空 state 基线 ──────────────────────────────────────
    st = quota.status()
    assert st.used == 0, f"used={st.used}"
    assert st.remaining == 5, f"remaining={st.remaining}"
    assert not st.exhausted
    assert st.wait_seconds() == 0.0

    # ── 2. 计数累加 ───────────────────────────────────────────
    for i in range(3):
        st = quota.record(f"u{i}@x.com")
    assert st.used == 3, f"used={st.used}"
    assert st.remaining == 2, f"remaining={st.remaining}"
    assert not st.exhausted
    assert state.is_file()
    assert len(state.read_text(encoding="utf-8").splitlines()) == 3

    # ── 3. check_or_raise 两种语义（此刻 used=3, limit=5）──────
    quota.check_or_raise(planned=2)          # 3+2 == 5，不超
    with pytest.raises(quota.QuotaExceeded):
        quota.check_or_raise(planned=3)      # 3+3 > 5，严格档该抛

    st2 = quota.check_or_raise(planned=3, allow_partial=True)
    assert st2.remaining == 2, f"remaining={st2.remaining}"

    # ── 4. 触顶 ───────────────────────────────────────────────
    quota.record("u3@x.com")
    st = quota.record("u4@x.com")            # 第 5 个 → 触顶
    assert st.exhausted, f"used={st.used}"
    assert st.remaining == 0
    assert st.wait_seconds() > 0, f"{st.wait_seconds() / 60:.1f} 分钟"
    assert "已用尽" in st.describe(), st.describe()

    with pytest.raises(quota.QuotaExceeded):
        quota.check_or_raise(planned=1, allow_partial=True)


# ── [4b] used > limit（补录超额）时的 wait_seconds ─────────────────────
def test_04b_overshoot_wait_uses_nth_record(env):
    """🔴 这一节是补的 —— 原来的自检只覆盖 `used == limit`，于是漏掉了
    "补录把计数推过上限"这种真实发生过的状态（实测 53/40）。
    那时最早一条滑出只让计数降 1，守卫仍然拦着，必须等到第
    `used - limit + 1` 条滑出。旧实现只取 oldest_ts，系统性**低估**等待。
    """
    state = env
    W = config.REG_QUOTA_WINDOW_H * 3600.0
    now = time.time()
    # 7 条记录，第 i 条（从 0 数）距过期还有 100*(i+1) 秒
    lines = []
    for i in range(7):
        lines.append(json.dumps({"ts": now - W + 100.0 * (i + 1),
                                 "email": f"over{i}@x.com"}))
    state.write_text("\n".join(lines) + "\n", encoding="utf-8")

    st = quota.status()
    assert st.used == 7, f"used={st.used}"
    assert st.exhausted
    assert st.must_expire == 3, f"must_expire={st.must_expire}"
    w = st.wait_seconds()
    assert 290 < w < 310, f"{w:.1f}s"          # 第 3 条滑出，不是第 1 条的 100s
    assert "超额 2 条" in st.describe() and "需滑出 3 条" in st.describe(), \
        st.describe()

    # 刚好等于 limit 时，只需滑出 1 条
    state.write_text("\n".join(lines[:5]) + "\n", encoding="utf-8")
    st = quota.status()
    assert st.must_expire == 1, f"must_expire={st.must_expire}"
    assert 90 < st.wait_seconds() < 110, f"{st.wait_seconds():.1f}s"

    # 未触顶时不该有等待
    state.write_text("\n".join(lines[:3]) + "\n", encoding="utf-8")
    st = quota.status()
    assert st.must_expire == 0
    assert st.wait_seconds() == 0.0


# ── [5]–[6] 一条状态链：并发写 100 行 → 追加坏行 ───────────────────────
def test_05_to_06_concurrent_append_then_bad_lines(env):
    state = env

    # ── 5. 并发 append 不丢行 ─────────────────────────────────
    # 这是**最要紧**的一条：多个 producer 线程各自 append，若用了
    # read-modify-write 就会互相覆盖。JSONL 追加写必须做到零丢失。
    n_threads, per = 20, 5
    barrier = threading.Barrier(n_threads)

    def hammer(tid: int):
        barrier.wait()                        # 尽量让写操作撞在一起
        for j in range(per):
            quota.record(f"t{tid}-{j}@x.com")

    ts = [threading.Thread(target=hammer, args=(i,)) for i in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    lines = [ln for ln in state.read_text(encoding="utf-8").splitlines() if ln.strip()]
    expect = n_threads * per
    assert len(lines) == expect, \
        f"丢了 {expect - len(lines)} 行" if len(lines) != expect else "零丢失"
    assert quota.status().used == expect, f"used={quota.status().used}"

    # ── 6. 半截行被跳过 ───────────────────────────────────────
    with state.open("a", encoding="utf-8") as f:
        f.write('{"ts": 1.0, "email": "half')  # 故意写半截
        f.write("\n")
        f.write("\n")                          # 空行
        f.write('not json at all\n')
    st = quota.status()
    assert st.used == expect, f"used={st.used}"
    assert st.used == expect                   # 坏行不被计入


# ── [7] 窗口外的旧记录不计入 ──────────────────────────────────────────
def test_07_out_of_window_records_excluded(env):
    state = env
    old_ts = time.time() - 7 * 3600            # 7h 前 > 6h 窗口
    with state.open("w", encoding="utf-8") as f:
        for i in range(10):
            f.write(json.dumps({"ts": old_ts, "email": f"old{i}@x.com"}) + "\n")
        f.write(json.dumps({"ts": time.time(), "email": "new@x.com"}) + "\n")

    st = quota.status()
    assert st.used == 1, f"used={st.used}"
    assert st.oldest_ts is not None and st.oldest_ts > time.time() - 60
    assert not st.exhausted                    # 旧记录不占额度


# ── [8] _compact_if_needed 重写文件 ───────────────────────────────────
def test_08_compact_rewrites_file(env):
    state = env
    old_ts = time.time() - 7 * 3600
    with state.open("w", encoding="utf-8") as f:
        for i in range(300):                   # 全部是窗口外的死记录
            f.write(json.dumps({"ts": old_ts, "email": f"dead{i}@x.com"}) + "\n")
    before = len(state.read_text(encoding="utf-8").splitlines())
    quota.record("fresh@x.com")                # 触发 compact
    after = len(state.read_text(encoding="utf-8").splitlines())
    assert after <= 2, f"before={before} after={after}"
    assert quota.status().used == 1, f"used={quota.status().used}"


# ── [9] state 不可写时 record 不抛异常 ────────────────────────────────
def test_09_oserror_does_not_crash(tmp_path, monkeypatch):
    """记不上不该拖垮主流程。"""
    monkeypatch.setattr(quota, "state_path",
                        lambda: tmp_path / "no" / "such" / "dir" / "x.jsonl")
    # 这里目录能被 mkdir 创建，所以不该崩；关键是**不能抛**
    quota.record("x@x.com")


# ── [10] 补录历史 ─────────────────────────────────────────────────────
def test_10_backfill_history(env, tmp_path):
    """本地计数是"功能上线后"才开始记的。此前注册过的一批若不补录，计数从 0
    开始 → 保护形同虚设。补录最容易错的地方有两个：
      ① 判据用 status=="success"（整链成功）而不是 register=="ok"
         → 漏掉"注册成功但登录失败"的账号，而它们**确实占了配额**
      ② 用"补录那一刻"当时间戳 → 两小时前的记录被算成"刚刚发生"
    """
    state = env
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
    hp = tmp_path / "results.json"
    hp.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")

    added, _dup = quota.backfill([str(hp)], dry_run=True)
    assert added == 3, f"added={added}"        # a/b/d
    assert not state.exists(), "dry-run 不该落盘"

    added, _dup = quota.backfill([str(hp)])
    assert added == 3, f"added={added}"
    assert quota.status().used == 2, f"used={quota.status().used}"  # a+b，d 在窗口外

    added, dup = quota.backfill([str(hp)])
    assert added == 0 and dup == 3, f"added={added} dup={dup}"
    assert quota.status().used == 2

    # zip 支持（历史备份就是 zip）
    zp = tmp_path / "hist.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("tmp/f.json", json.dumps(
            [{"email": "f@x.com", "created_at": ca(1), "status": "success",
              "stages": {"register": "ok"}}], ensure_ascii=False))
        z.writestr("readme.txt", "not json")
    added, _dup = quota.backfill([str(zp)])
    assert added == 1, f"added={added}"
    assert quota.status().used == 3, f"used={quota.status().used}"

    added, _dup = quota.backfill([str(tmp_path / "nope.json")])
    assert added == 0, f"added={added}"


# ── [11] 出口作用域（scope）隔离 ──────────────────────────────────────
def test_11_scope_isolation(scope_env):
    """槽位池模式下每个出口是**独立配额**，计数必须按出口分开。"""
    state2 = scope_env

    # 11a. 无 scope 的历史记录属于"老出口"，不该被新槽位看到
    state2.write_text(
        json.dumps({"ts": time.time(), "email": "old@x.com"}) + "\n"
        + json.dumps({"ts": time.time(), "email": "old2@x.com"}) + "\n",
        encoding="utf-8")
    assert quota.status().used == 2, f"used={quota.status().used}"
    assert quota.status(scope="slot1").used == 0, \
        f"used={quota.status(scope='slot1').used}"
    assert quota.status(scope="").used == 2, f"used={quota.status(scope='').used}"

    # 11b. 各 scope 独立累加、互不影响
    quota.record("a@x.com", scope="slot1")
    quota.record("b@x.com", scope="slot2")
    quota.record("c@x.com", scope="slot2")
    assert quota.status(scope="slot1").used == 1, \
        f"used={quota.status(scope='slot1').used}"
    assert quota.status(scope="slot2").used == 2, \
        f"used={quota.status(scope='slot2').used}"
    assert quota.status(scope="slot3").used == 0, \
        f"used={quota.status(scope='slot3').used}"
    assert quota.status().used == 5, f"used={quota.status().used}"  # 2 老 + 3 新
    assert quota.record("d@x.com", scope="slot1").used == 2, \
        "slot1 记第 2 条后应为 2"

    # 11c. 一个出口触顶不影响别的出口 —— 这是整个 scope 机制的目的
    assert quota.status(scope="slot1").exhausted            # 2/2
    assert quota.status(scope="slot2").exhausted            # 2/2
    assert not quota.status(scope="slot3").exhausted        # 0/2，不被拖累

    with pytest.raises(quota.QuotaExceeded):
        quota.check_or_raise(planned=1, scope="slot1")
    quota.check_or_raise(planned=1, scope="slot3")          # 该出口还有余量，不抛

    # 11d. 向后兼容：老文件里的行没有 scope 字段，解析不能崩
    assert quota.status(scope="").used == 2, f"used={quota.status(scope='').used}"
