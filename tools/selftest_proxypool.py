"""`src/proxypool.py` 自检 —— 不碰网络，只用假槽位串。

为什么要有这个文件
------------------
槽位池管的是**出口 IP 的分配**。它出错的方式全是静默的：

  - 均衡分配坏了 → 少数出口被反复用 → 更快撞穿那个 IP 的配额（不报错）
  - 冷却没生效   → 刚被判封的出口马上又被分配出去（不报错）
  - 归还漏了     → 池子越跑越小，最后全忙 → 批量任务集体超时（很晚才发现）
  - `exclude` 失效 → worker 在几个坏出口之间弹跳，而不是向前推进

所以每条规则都要能单独验。

跑法：
    python tools/selftest_proxypool.py
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config  # noqa: E402
from src.proxypool import (  # noqa: E402
    NoEligibleSlot, ProxySlotPool, build_pool,
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"   [{detail}]" if detail else ""))


def main() -> int:
    slots = [f"http://127.0.0.1:790{i}" for i in range(1, 5)]   # 4 个假槽位

    # ── 1. 基本属性与空池保护 ─────────────────────────────────
    print("[1] 基本属性")
    try:
        ProxySlotPool([])
        check("空槽位列表抛 ValueError", False, "没抛")
    except ValueError:
        check("空槽位列表抛 ValueError", True)

    p = ProxySlotPool(slots, cooldown=120)
    check("size == 4", p.size == 4, f"size={p.size}")
    check("url_of(1) 正确", p.url_of(1) == slots[0], p.url_of(1))

    # ── 2. 均衡分配：优先最少被用过的 ──────────────────────────
    print("\n[2] 均衡分配（不是简单轮询）")
    leases = [p.acquire() for _ in range(4)]
    check("4 次 acquire 拿到 4 个**不同**槽位",
          len({l.slot for l in leases}) == 4,
          str(sorted(l.slot for l in leases)))
    check("按最少使用排序 → 恰好是 1,2,3,4",
          sorted(l.slot for l in leases) == [1, 2, 3, 4])
    for l in leases:
        p.release(l)

    # 再拿 4 个：此时 _uses 全为 1，应回到 1,2,3,4（同分时按槽位号）
    leases2 = [p.acquire() for _ in range(4)]
    check("第二轮同样拿满 4 个不同槽位",
          len({l.slot for l in leases2}) == 4)
    check("累计使用次数均衡（每个都是 2）",
          set(p.stats()["uses"].values()) == {2}, str(p.stats()["uses"]))
    for l in leases2:
        p.release(l)

    # ── 3. 全忙时阻塞、超时抛 TimeoutError ────────────────────
    print("\n[3] 全忙阻塞 + 超时")
    busy = [p.acquire() for _ in range(4)]
    t0 = time.monotonic()
    try:
        p.acquire(timeout=0.4)
        check("全忙时 acquire 超时抛 TimeoutError", False, "没抛")
    except TimeoutError:
        check("全忙时 acquire 超时抛 TimeoutError", True,
              f"{time.monotonic() - t0:.2f}s")
    for l in busy:
        p.release(l)

    # ── 4. 归还后立刻可再用（不泄漏）──────────────────────────
    print("\n[4] 归还后立刻可用")
    l = p.acquire()
    p.release(l)
    t0 = time.monotonic()
    l2 = p.acquire(timeout=0.5)
    dt = time.monotonic() - t0
    # ⚠ 不能断言"拿回同一个槽位" —— 均衡分配会**故意**换一个累计使用更少的
    #   （刚归还的那个 uses 刚 +1，自然排在后面）。这里只验"没阻塞、拿得到"。
    check("归还后能马上再取到（不阻塞）", l2 is not None and dt < 0.2,
          f"slot={l2.slot if l2 else None} {dt:.3f}s")
    check("拿到的是合法槽位号", l2 is not None and 1 <= l2.slot <= 4)
    p.release(l2)
    check("两次归还后 4 个槽位全空闲（无泄漏）", p.stats()["free"] == 4,
          f"free={p.stats()['free']}")

    # ── 5. 重复归还是幂等的 ───────────────────────────────────
    print("\n[5] 重复归还幂等")
    l = p.acquire()
    p.release(l)
    free_after_first = p.stats()["free"]
    p.release(l)                                  # 再来一次
    check("重复 release 不改变空闲数",
          p.stats()["free"] == free_after_first,
          f"{free_after_first} -> {p.stats()['free']}")

    # ── 6. 封禁 → 长冷却；冷却期内不再分配 ────────────────────
    print("\n[6] report_banned → 长冷却")
    p2 = ProxySlotPool(slots, cooldown=120)
    a = p2.acquire()
    p2.report_banned(a, "B0000 测试")
    check("report_banned 后该槽位进冷却",
          a.slot in p2.stats()["cooling"], str(p2.stats()["cooling"]))
    check("封禁计数 +1", p2.stats()["bans"].get(a.slot) == 1)
    # 连续取 3 个，都不该是被封的那个
    got = [p2.acquire().slot for _ in range(3)]
    check("冷却中的槽位不再被分配", a.slot not in got, str(got))
    check("冷却期内只剩 3 个可用", len(got) == 3)

    # ── 7. 冷却到期后自动恢复（不永久拉黑）──────────────────
    print("\n[7] 冷却到期自动恢复")
    p3 = ProxySlotPool(slots, cooldown=0.3)
    b = p3.acquire()
    p3.report_banned(b)
    time.sleep(0.45)
    check("冷却到期后冷却表为空", not p3.stats()["cooling"])
    c = p3.acquire(timeout=0.5)
    check("到期后能再取到槽位", c is not None, f"slot={c.slot if c else None}")

    # ── 8. report_failed 用短冷却（不能和封禁同长）────────────
    print("\n[8] report_failed 短冷却")
    p4 = ProxySlotPool(slots, cooldown=120)
    d = p4.acquire()
    p4.report_failed(d, "timeout", cooldown=0.3)
    check("网络失败的冷却远短于封禁",
          p4.stats()["cooling"][d.slot] < 1.0,
          f"{p4.stats()['cooling'][d.slot]}s vs 封禁 120s")
    check("短冷却不计入封禁次数", d.slot not in p4.stats()["bans"])

    # ── 9. exclude：worker 向前推进，不回到试过的槽位 ──────────
    print("\n[9] exclude 让 worker 向前推进")
    p5 = ProxySlotPool(slots, cooldown=120)
    e1 = p5.acquire()
    p5.report_banned(e1)                     # 假设被封
    e2 = p5.acquire(exclude={e1.slot})
    check("exclude 生效：拿到的是别的槽位", e2.slot != e1.slot,
          f"{e1.slot} -> {e2.slot}")
    # 排除掉 3 个，只剩 1 个可选
    p5.release(e2)
    only = p5.acquire(exclude={1, 2, 3})
    check("排除到只剩一个时仍能取到", only.slot == 4, f"slot={only.slot}")

    # ── 10. 并发：不重不漏 ────────────────────────────────────
    print("\n[10] 并发 acquire/release 不重不漏")
    p6 = ProxySlotPool(slots, cooldown=120)
    seen, lock = [], threading.Lock()
    errors = []

    def worker():
        try:
            for _ in range(25):
                le = p6.acquire(timeout=5)
                with lock:
                    seen.append(le.slot)
                time.sleep(0.001)
                p6.release(le)
        except Exception as ex:                                # noqa: BLE001
            errors.append(f"{type(ex).__name__}: {ex}")

    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("并发无异常", not errors, str(errors[:2]))
    check("共 100 次租约", len(seen) == 100, f"n={len(seen)}")
    check("每次都拿到合法槽位号", set(seen) <= {1, 2, 3, 4}, str(sorted(set(seen))))
    check("结束后 4 个槽位全空闲（无泄漏）", p6.stats()["free"] == 4,
          f"free={p6.stats()['free']}")

    # ── 11. build_pool 的"未配置就退回旧行为" ─────────────────
    print("\n[11] build_pool：未配置时返回 None")
    old = config.proxy_slots
    config.proxy_slots = lambda: []                # type: ignore[assignment]
    check("未配置槽位 → None（不改变旧行为）", build_pool() is None)
    config.proxy_slots = lambda: slots             # type: ignore[assignment]
    bp = build_pool()
    check("配了槽位 → 建出 4 个槽位的池", bp is not None and bp.size == 4)
    config.proxy_slots = old                       # type: ignore[assignment]

    # ── 12. describe 不撒谎（槽位数 vs 出口数要分开）───────────
    print("\n[12] describe 只讲槽位，不假装知道出口数")
    txt = ProxySlotPool(slots, cooldown=120).describe()
    check("describe 含槽位数", "4 个槽位" in txt, txt)
    check("describe 不声称出口 IP 个数",
          "出口 IP" not in txt and "出口数" not in txt, txt)

    # ── 13. accept 过滤器：跳过"不该用"的槽位 ───────────────────
    #     本项目用它跳过"出口 IP 配额已满"的槽位。不加的话池子会按
    #     "用得最少"均分，已满的槽位白吃一大半租约。
    print("\n[13] accept 过滤器")
    p7 = ProxySlotPool(slots, cooldown=120)
    # 只有 slot4 合格
    only4 = lambda i: i == 4                                  # noqa: E731
    got = []
    for _ in range(3):
        lz = p7.acquire(timeout=1, accept=only4)
        got.append(lz.slot)
        p7.release(lz)                       # 用完必须归还，否则第二次就等不到了
    check("accept 只放行合格槽位", set(got) == {4}, str(got))

    # 🔴 关键回归：合格槽位被占用时**必须等待**，不能判成"全都不合格"。
    #    这是本项目实际踩过的坑 —— 第一版把放弃条件写成"当前空闲的
    #    里面没有合格的"，结果注册高峰期 50 个任务只有头 3 个真跑了：
    #    那一刻 3 个槽位正被占用，唯一空闲的 slot1 恰好是配额满的。
    p8 = ProxySlotPool(slots, cooldown=120)
    held = p8.acquire(timeout=1, accept=only4)      # 先占住唯一合格的 slot4
    try:
        p8.acquire(timeout=0.3, accept=only4)       # 现在没有空闲的合格槽位了
        check("唯一合格槽位被占用时应超时等待（不是 NoEligibleSlot）",
              False, "居然拿到了？")
    except NoEligibleSlot as ex:
        check("唯一合格槽位被占用时应超时等待（不是 NoEligibleSlot）",
              False, f"误判成 NoEligibleSlot：{ex}")
    except TimeoutError:
        check("唯一合格槽位被占用时应超时等待（不是 NoEligibleSlot）", True)
    finally:
        p8.release(held)

    # 池子里一个合格的都没有 → 立刻抛 NoEligibleSlot（不空等 timeout）
    p9 = ProxySlotPool(slots, cooldown=120)
    t0 = time.monotonic()
    try:
        p9.acquire(timeout=30, accept=lambda i: False)
        check("全都不合格 → 立刻 NoEligibleSlot", False, "没抛异常")
    except NoEligibleSlot:
        dt = time.monotonic() - t0
        check("全都不合格 → 立刻 NoEligibleSlot", dt < 1.0,
              f"耗时 {dt:.2f}s（应远小于 timeout=30s）")

    # accept 为空时行为与老版本完全一致（不能改变未使用该特性的调用方）
    p10 = ProxySlotPool(slots, cooldown=120)
    got10 = {p10.acquire(timeout=1).slot for _ in range(4)}
    check("accept=None 时行为不变（4 个槽位都能拿到）",
          got10 == {1, 2, 3, 4}, str(sorted(got10)))

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
