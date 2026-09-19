"""`src/proxypool.py` 的行为测试 —— **不碰网络、不碰真实槽位**。

为什么单独成文件
----------------
槽位池管的是**出口 IP 的分配**，它出错的方式全是静默的：

  - 均衡分配坏了 → 少数出口被反复用 → 更快撞穿那个 IP 的配额（不报错）
  - 冷却没生效   → 刚被判封的出口马上又被分配出去（不报错）
  - 归还漏了     → 池子越跑越小，最后全忙 → 批量任务集体超时（很晚才发现）
  - 同 IP 互斥坏了 → 同一出口被并发使用 → 撞穿速度翻倍（不报错）

所以每条规则都要能单独验。

跑法：
    pytest tests/test_proxypool.py -v
"""

import socket
import threading
import time

import pytest

from src import config
from src.proxypool import (
    AllSlotsDead,
    NoEligibleSlot,
    ProxySlotPool,
    _host_port,
    build_pool,
    check_slots_alive,
)

SLOTS = [f"http://127.0.0.1:790{i}" for i in range(1, 5)]     # 4 个假槽位
# 6 槽位 / 4 出口 —— 复现实测的真实形态（`memory/2026-09-19.md`）
SIX_SLOTS = [f"http://127.0.0.1:790{i}" for i in range(1, 7)]
SIX_IPS = {1: "A", 2: "A", 3: "B", 4: "B", 5: "C", 6: "D"}


# ────────────────────────────────────────────────────────────────
# 基本属性与空池保护
# ────────────────────────────────────────────────────────────────
def test_empty_slots_rejected():
    with pytest.raises(ValueError):
        ProxySlotPool([])


def test_basic_properties():
    p = ProxySlotPool(SLOTS, cooldown=120)
    assert p.size == 4
    assert p.url_of(1) == SLOTS[0]
    assert p.url_of(4) == SLOTS[3]


# ────────────────────────────────────────────────────────────────
# 均衡分配：优先用"累计使用最少"的，而不是简单轮询
# ────────────────────────────────────────────────────────────────
def test_balanced_allocation():
    p = ProxySlotPool(SLOTS, cooldown=120)
    leases = [p.acquire() for _ in range(4)]
    assert len({le.slot for le in leases}) == 4, "4 次 acquire 应拿到 4 个不同槽位"
    assert [le.slot for le in leases] == [1, 2, 3, 4], "按最少使用排序"
    for le in leases:
        p.release(le)

    leases2 = [p.acquire() for _ in range(4)]
    assert len({le.slot for le in leases2}) == 4
    assert set(p.stats()["uses"].values()) == {2}, "累计使用次数应均衡"


def test_acquire_times_out_when_all_busy():
    p = ProxySlotPool(SLOTS, cooldown=120)
    busy = [p.acquire() for _ in range(4)]
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        p.acquire(timeout=0.4)
    assert time.monotonic() - t0 < 2.0
    for le in busy:
        p.release(le)


# ────────────────────────────────────────────────────────────────
# 归还：幂等、不漏
# ────────────────────────────────────────────────────────────────
def test_release_is_idempotent():
    p = ProxySlotPool(SLOTS, cooldown=120)
    lease = p.acquire()
    p.release(lease)
    assert p.stats()["free"] == 4
    p.release(lease)                       # 重复归还
    assert p.stats()["free"] == 4, "重复 release 不该改变空闲数"

    lease2 = p.acquire(timeout=0.5)
    assert lease2 is not None, "归还后应能马上再取到（不阻塞）"
    assert 1 <= lease2.slot <= 4


def test_no_leak_under_concurrency():
    p = ProxySlotPool(SLOTS, cooldown=120)
    seen, errors = [], []

    def worker():
        try:
            for _ in range(25):
                le = p.acquire(timeout=5)
                seen.append(le.slot)
                p.release(le)
        except Exception as ex:              # noqa: BLE001
            errors.append(repr(ex))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, str(errors[:2])
    assert len(seen) == 100, f"共 100 次租约，实际 {len(seen)}"
    assert set(seen) <= {1, 2, 3, 4}
    assert p.stats()["free"] == 4, "结束后 4 个槽位必须全空闲（无泄漏）"


# ────────────────────────────────────────────────────────────────
# 冷却：封禁 vs 网络失败，时长必须分开
# ────────────────────────────────────────────────────────────────
def test_ban_and_network_failure_have_different_cooldowns():
    p = ProxySlotPool(SLOTS, cooldown=120)
    a = p.acquire()
    p.report_banned(a, "B0000")
    assert a.slot in p.stats()["cooling"], "被封的槽位应进冷却"
    assert p.stats()["bans"].get(a.slot) == 1

    got = [p.acquire(timeout=0.3).slot for _ in range(3)]
    assert a.slot not in got, "冷却中的槽位不该被分配"
    assert len(got) == 3, "冷却期内只剩 3 个可用（第 4 个被冷却占着）"

    p2 = ProxySlotPool(SLOTS, cooldown=120)
    b = p2.acquire()
    p2.report_failed(b, "timeout")
    assert b.slot not in p2.stats()["bans"], "网络失败不计入封禁次数"
    net_cool = p2.stats()["cooling"][b.slot]
    ban_cool = p.stats()["cooling"][a.slot]
    assert net_cool < ban_cool / 2, "网络失败的冷却必须远短于封禁"


def test_cooldown_expires_and_slot_returns():
    p = ProxySlotPool(SLOTS, cooldown=0.2)
    a = p.acquire()
    p.report_banned(a)
    time.sleep(0.25)
    assert not p.stats()["cooling"], "冷却到期后冷却表应为空"
    assert p.acquire(timeout=0.5) is not None


# ────────────────────────────────────────────────────────────────
# 🔴 冷却**指数退避**（阶段一新增）
#
# 修的是参数失配：基础冷却 120s vs 服务端配额窗口 24h（差 720 倍），
# 且封禁不写配额台账 ⇒ 120s 后 accept 照样放行 ⇒ 被封的出口
# 每 120s 被重新租出去打一次注定失败的请求（一天约 720 次）。
# ────────────────────────────────────────────────────────────────
def test_ban_cooldown_backs_off_exponentially():
    p = ProxySlotPool(SLOTS, cooldown=100, cooldown_max=1000)
    only1 = lambda i: i == 1                                     # noqa: E731
    seen = []
    for _ in range(5):
        # 手工清掉冷却，避免真等 —— 这里考的是**退避算法**，不是等待行为。
        # （读私有状态是有意的：这是对调度策略的白盒验证。）
        with p._cond:
            p._cool_until.pop(1, None)
        lease = p.acquire(timeout=1, accept=only1)
        p.report_banned(lease, "B0000")
        with p._cond:
            seen.append(round(_cool_left(p)))

    assert seen == [100, 200, 400, 800, 1000], (
        f"退避序列应为 100→200→400→800→封顶1000，实际 {seen}")


def _cool_left(p, slot: int = 1) -> float:
    """槽位 `slot` 还剩多少秒冷却。

    🔴 减的是 `time.time()`（**墙钟**），不是 `time.monotonic()` ——
    `_cool_until` 存的是 epoch，因为这份状态要**跨进程**读写
    （落盘再读回，见 `proxypool.state_path()` 的说明）。
    减错钟不会报错，只会得到一个 `1.7e9` 量级的荒谬值 ——
    2026-09-19 改持久化时这三个用例就是这么红的。
    """
    with p._cond:
        return p._cool_until[slot] - time.time()


def test_network_failure_does_not_back_off():
    """网络抖动是随机的，不是"这个出口越来越坏"的证据 —— 不退避。"""
    p = ProxySlotPool(SLOTS, cooldown=100, cooldown_max=1000)
    only1 = lambda i: i == 1                                     # noqa: E731
    seen = []
    for _ in range(3):
        with p._cond:
            p._cool_until.pop(1, None)
        lease = p.acquire(timeout=1, accept=only1)
        p.report_failed(lease, "timeout", cooldown=20.0)
        with p._cond:
            seen.append(round(_cool_left(p)))
    assert seen == [20, 20, 20], f"网络失败应保持固定冷却，实际 {seen}"


def test_cooldown_backoff_capped_by_config_default():
    """默认封顶 6h —— 退避不该把出口永久踢出池子。"""
    assert config.IR_PROXY_COOLDOWN_MAX == 21600
    p = ProxySlotPool(SLOTS)             # 全默认：base=120, cap=21600
    only1 = lambda i: i == 1                                     # noqa: E731
    for _ in range(12):
        with p._cond:
            p._cool_until.pop(1, None)
        lease = p.acquire(timeout=1, accept=only1)
        p.report_banned(lease)
    with p._cond:
        left = _cool_left(p)
    assert abs(left - 21600) < 5, f"应封顶在 21600s，实际 {left:.0f}"


# ────────────────────────────────────────────────────────────────
# 🔴 同出口 IP 互斥（阶段一新增，模块 docstring 规则 4）
# ────────────────────────────────────────────────────────────────
def test_same_egress_ip_not_leased_twice():
    ips = {1: "10.0.0.1", 2: "10.0.0.1", 3: "10.0.0.2", 4: "10.0.0.2"}
    p = ProxySlotPool(SLOTS, cooldown=120, slot_ips=ips)
    a = p.acquire(timeout=1)
    b = p.acquire(timeout=1)
    assert ips[a.slot] != ips[b.slot], (
        f"两个同出口 IP 的槽位被同时租出：slot{a.slot} / slot{b.slot}")

    # 两个出口都被占，第三个必须**等**（不能复用同 IP，也不能误判成"没有合格槽位"）
    with pytest.raises(TimeoutError):
        p.acquire(timeout=0.3)

    p.release(a)
    p.release(b)
    assert p.stats()["ip_held"] == {}, "全部归还后 ip_held 必须清空"


def test_effective_concurrency_equals_distinct_egress():
    """6 槽位 / 4 出口 ⇒ 并发上限是 4，不是 6。"""
    p = ProxySlotPool(SIX_SLOTS, cooldown=120, slot_ips=SIX_IPS)
    assert p.distinct_egress == 4
    held = [p.acquire(timeout=1) for _ in range(4)]
    assert len({SIX_IPS[le.slot] for le in held}) == 4
    with pytest.raises(TimeoutError):
        p.acquire(timeout=0.3)           # 第 5 个必须等
    for le in held:
        p.release(le)


def test_same_egress_ip_never_concurrent_under_stress():
    """并发压力下也不允许同一出口 IP 同时有两个租约在外的。"""
    p = ProxySlotPool(SIX_SLOTS, cooldown=120, slot_ips=SIX_IPS)
    lock = threading.Lock()
    inflight, violations, peak = [], [], []

    def worker():
        for _ in range(15):
            try:
                lease = p.acquire(timeout=2)
            except (TimeoutError, NoEligibleSlot):
                continue
            ip = SIX_IPS[lease.slot]
            with lock:
                if ip in {x[1] for x in inflight}:
                    violations.append((lease.slot, ip, list(inflight)))
                inflight.append((lease.slot, ip))
                peak.append(len({x[1] for x in inflight}))
            time.sleep(0.001)
            with lock:
                inflight.remove((lease.slot, ip))
            p.release(lease)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not violations, f"同出口 IP 并发！{violations[:2]}"
    assert peak and max(peak) <= 4, f"并发上限应 ≤ 4，实际峰值 {max(peak)}"
    assert p.stats()["ip_held"] == {}, "压力测试后 ip_held 必须归零（无泄漏）"


@pytest.mark.parametrize("action", ["release", "banned", "failed"])
def test_ip_held_returns_to_zero_on_every_path(action):
    """三条归还路径都必须对称地减 `_ip_held`，且幂等。

    🔴 一旦减成负数，`_pick_locked` 的 `== 0` 判据就永远为假，
    那个出口被**永久锁死**，症状是"池子莫名少一个出口"，极难排查。
    """
    ips = {1: "10.0.0.1", 2: "10.0.0.1", 3: "10.0.0.2", 4: "10.0.0.2"}
    p = ProxySlotPool(SLOTS, cooldown=0.01, cooldown_max=0.02, slot_ips=ips)
    lease = p.acquire(timeout=1)
    ip = ips[lease.slot]
    assert p.stats()["ip_held"][ip] == 1

    call = {"release": p.release, "banned": p.report_banned,
            "failed": p.report_failed}[action]
    call(lease)
    assert p.stats()["ip_held"] == {}, f"{action} 之后 ip_held 没归零"
    call(lease)                          # 幂等：重复归还
    assert p.stats()["ip_held"] == {}, f"{action} 重复调用后出现负计数"


def test_partial_egress_mapping_disables_mutex_entirely():
    """映射不全时**整体**退回按槽位分配 —— 不做"部分互斥"。

    部分互斥等于给同一个 IP 开个后门：哪些槽位受保护取决于配置完整性，
    比完全不互斥更难排查。
    """
    logs = []
    ips = {1: "10.0.0.1", 2: "10.0.0.1"}          # 只有 2/4 个槽位有映射
    p = ProxySlotPool(SLOTS, cooldown=120, slot_ips=ips, log=logs.append)
    assert p.distinct_egress is None
    assert any("整体退回" in m for m in logs), f"应打印告警，实际 {logs}"
    leases = [p.acquire(timeout=1) for _ in range(4)]
    assert len({le.slot for le in leases}) == 4, "退回后 4 个槽位应可同时租出"


def test_no_mapping_keeps_old_behaviour():
    """未登记映射时行为与加互斥之前**完全一致**。"""
    p = ProxySlotPool(SLOTS, cooldown=120)
    assert p.distinct_egress is None
    assert p.egress_of(1) == ""
    got = {p.acquire(timeout=1).slot for _ in range(4)}
    assert got == {1, 2, 3, 4}


# ────────────────────────────────────────────────────────────────
# accept 过滤器：跳过"不该用"的槽位（配额已满）
# ────────────────────────────────────────────────────────────────
def test_accept_filter_only_eligible_slots():
    p = ProxySlotPool(SLOTS, cooldown=120)
    only4 = lambda i: i == 4                                     # noqa: E731
    got = []
    for _ in range(3):
        le = p.acquire(timeout=1, accept=only4)
        got.append(le.slot)
        p.release(le)
    assert set(got) == {4}


def test_eligible_slot_occupied_waits_instead_of_giving_up():
    """🔴 关键回归：合格槽位被占用时**必须等待**，不能判成"全都不合格"。

    本项目实际踩过的坑 —— 第一版把放弃条件写成"当前空闲的里面没有合格的"，
    结果注册高峰期 50 个任务只有头 3 个真跑了。
    """
    p = ProxySlotPool(SLOTS, cooldown=120)
    only4 = lambda i: i == 4                                     # noqa: E731
    held = p.acquire(timeout=1, accept=only4)
    try:
        with pytest.raises(TimeoutError):       # 不是 NoEligibleSlot
            p.acquire(timeout=0.3, accept=only4)
    finally:
        p.release(held)


def test_all_ineligible_raises_immediately():
    """池子里一个合格的都没有 → 立刻抛，不空等 timeout。"""
    p = ProxySlotPool(SLOTS, cooldown=120)
    t0 = time.monotonic()
    with pytest.raises(NoEligibleSlot):
        p.acquire(timeout=30, accept=lambda i: False)
    assert time.monotonic() - t0 < 1.0, "应立刻抛，不该等满 30s"


def test_exclude_makes_worker_advance():
    p = ProxySlotPool(SLOTS, cooldown=120)
    e1 = p.acquire()
    e2 = p.acquire(exclude={e1.slot})
    assert e2.slot != e1.slot
    p.release(e1)
    only = p.acquire(exclude={1, 2, 3})
    assert only.slot == 4


# ────────────────────────────────────────────────────────────────
# describe 不撒谎：槽位数 ≠ 出口数
# ────────────────────────────────────────────────────────────────
def test_describe_does_not_claim_unknown_concurrency():
    txt = ProxySlotPool(SLOTS, cooldown=120).describe()
    assert "4 个槽位" in txt
    assert "并发上限" not in txt, f"算不出并发上限就不该报：{txt}"
    assert "未登记" in txt, f"没映射时必须主动告警，沉默会被读成『互斥已生效』：{txt}"


def test_describe_reports_concurrency_equals_distinct_egress():
    ips = {1: "10.0.0.1", 2: "10.0.0.1", 3: "10.0.0.2", 4: "10.0.0.2"}
    txt = ProxySlotPool(SLOTS, cooldown=120, slot_ips=ips).describe()
    assert "并发上限 2" in txt, txt


# ────────────────────────────────────────────────────────────────
# 🔴 端口预检（阶段一新增，补上 IR_PROXY_PREFLIGHT 承诺的行为）
# ────────────────────────────────────────────────────────────────
def test_host_port_parsing():
    assert _host_port("http://127.0.0.1:7901") == ("127.0.0.1", 7901)
    assert _host_port("127.0.0.1:7901") == ("127.0.0.1", 7901)
    assert _host_port("http://user:pass@203.0.113.5:8080") == ("203.0.113.5", 8080)
    assert _host_port("  http://127.0.0.1:7901  ") == ("127.0.0.1", 7901)
    with pytest.raises(ValueError):
        _host_port("http://127.0.0.1")            # 没有端口
    with pytest.raises(ValueError):
        _host_port("http://127.0.0.1:notaport")


def _free_port() -> int:
    """拿一个**确定没人监听**的端口。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_check_slots_alive_marks_dead_port():
    port = _free_port()
    alive, dead = check_slots_alive([f"http://127.0.0.1:{port}"])
    assert not alive and dead == [f"http://127.0.0.1:{port}"]


def test_check_slots_alive_detects_listening_port():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    try:
        alive, dead = check_slots_alive([url], timeout=1.0)
        assert alive == [url] and not dead
    finally:
        srv.close()


def test_check_slots_alive_unparsable_goes_to_dead():
    alive, dead = check_slots_alive(["这不是一个代理串"])
    assert not alive and dead == ["这不是一个代理串"]


def test_build_pool_raises_when_all_slots_dead(monkeypatch):
    """配了槽位但一个端口都没监听 → **大声失败**，不静默退回单代理。

    🔴 静默退回的后果：每条记录都以代理连接错误收场，而"注册全失败"
    在本项目里最容易被误读成"换 IP 也不行 / 还在封" —— 结论完全错。
    """
    port = _free_port()
    monkeypatch.setattr(config, "proxy_slots",
                        lambda: [f"http://127.0.0.1:{port}"])
    with pytest.raises(AllSlotsDead) as ei:
        build_pool()
    msg = str(ei.value)
    assert "没有" in msg and "proxypool_ctl" in msg, msg


def test_build_pool_preflight_can_be_skipped(monkeypatch):
    """自检 / 离线测试必须能跳过预检（否则假槽位会误报）。"""
    port = _free_port()
    monkeypatch.setattr(config, "proxy_slots",
                        lambda: [f"http://127.0.0.1:{port}"])
    pool = build_pool(preflight=False)
    assert pool is not None and pool.size == 1


def test_build_pool_none_when_no_slots(monkeypatch):
    monkeypatch.setattr(config, "proxy_slots", lambda: [])
    assert build_pool(preflight=False) is None, "未配置槽位必须返回 None"


def test_build_pool_enables_mutex_from_config(monkeypatch):
    """端口 → 出口 IP 映射齐了，就自动启用互斥。"""
    monkeypatch.setattr(
        config, "proxy_slots",
        lambda: ["http://127.0.0.1:7901", "http://127.0.0.1:7902"])
    monkeypatch.setattr(config, "SLOT_EGRESS_IPS",
                        {"7901": "203.0.113.11", "7902": "203.0.113.11"})
    pool = build_pool(preflight=False)
    assert pool is not None
    assert pool.distinct_egress == 1, "两个槽位同出口 ⇒ 并发上限 1"


def test_build_pool_falls_back_when_mapping_missing(monkeypatch):
    """映射缺失时**不报错**，只是不启用互斥 —— 与加这个功能之前一致。"""
    logs = []
    monkeypatch.setattr(
        config, "proxy_slots",
        lambda: ["http://127.0.0.1:7901", "http://127.0.0.1:7902"])
    monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {})
    pool = build_pool(preflight=False, log=logs.append)
    assert pool is not None and pool.distinct_egress is None
    assert any("整体不启用" in m for m in logs), f"应告警，实际 {logs}"


# ────────────────────────────────────────────────────────────────
# 状态持久化（2026-09-19 新增）
# ────────────────────────────────────────────────────────────────
# 为什么必须持久化：`report_banned` 的冷却按 2 的幂退避（120s → 6h），
# 而服务端配额窗口是 24h。进程一退退避就重置回 120s —— 每重跑一次批量，
# 就在同一个被封的出口上重新撞一遍。
#
# 状态路径由 `tests/conftest.py` 的 autouse 夹具重定向到 tmp 目录，
# 所以下面这些用例都直接读写 `proxypool.state_path()`。


def _state() -> dict:
    import json

    from src.proxypool import state_path
    p = state_path()
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def test_ban_cooldown_survives_a_new_pool():
    """🔴 本次改造的核心：进程重来，冷却还在。"""
    p = ProxySlotPool(SLOTS, cooldown=100, cooldown_max=10000)
    only1 = lambda i: i == 1                                     # noqa: E731
    p.report_banned(p.acquire(timeout=1, accept=only1), "B0000")

    # 换一个"新进程"（新对象），状态从磁盘读回
    p2 = ProxySlotPool(SLOTS, cooldown=100, cooldown_max=10000)
    assert _cool_left(p2) > 90, "冷却必须跨进程保留"
    assert p2.stats()["bans"] == {1: 1}, "封禁次数也必须保留"


def test_backoff_continues_across_pools():
    """退避档位跨进程累积 —— 不然每轮批量都从第一档 120s 重来。"""
    only1 = lambda i: i == 1                                     # noqa: E731
    seen = []
    for _ in range(4):
        p = ProxySlotPool(SLOTS, cooldown=100, cooldown_max=10000)
        with p._cond:                # 手工清冷却，考的是**退避算法**不是等待
            p._cool_until.pop(1, None)
        p.report_banned(p.acquire(timeout=1, accept=only1))
        seen.append(round(_cool_left(p)))

    assert seen == [100, 200, 400, 800], f"跨进程退避应继续递增，实际 {seen}"


def test_bans_survive_expired_cooldown(tmp_path, monkeypatch):
    """冷却过期了，**封禁次数仍要留** —— 它决定下一档退避。

    只留冷却不留次数的话，冷却一结束退避就退回第一档，
    等于"封得越多次、恢复得越快"，正好反了。
    """
    import json

    from src.proxypool import state_path
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps({
        "version": 1, "saved_at": 0.0,
        "slots": {"127.0.0.1:7901": {"cool_until": 1.0, "bans": 5,
                                     "reason": "B0000 @ a@example.com"}},
    }), encoding="utf-8")

    p = ProxySlotPool(SLOTS, cooldown=100, cooldown_max=100000)
    assert p.stats()["bans"] == {1: 5}, "过期的冷却不该带走封禁次数"
    with p._cond:
        assert 1 not in p._cool_until, "过期的冷却不该被当成还在冷却"

    # 再封一次 → 第 6 次，冷却应是 100 * 2**5 = 3200，而不是 100
    p.report_banned(p.acquire(timeout=1, accept=lambda i: i == 1))
    assert 3100 < _cool_left(p) < 3300, f"退避档位丢了：{_cool_left(p):.0f}"


def test_state_keyed_by_host_port_not_index(tmp_path, monkeypatch):
    """🔴 槽位顺序变了，冷却必须跟着**出口**走，不能跟着位置号走。

    按位置号存的话，`slots.txt` 增删一条会让所有冷却静默错配到别的出口上 ——
    与本项目 `config.SLOT_EGRESS_IPS` 记的那个坑同形（已实测发生过一次）。
    """
    import json

    from src.proxypool import state_path
    state_path().parent.mkdir(parents=True, exist_ok=True)
    # 7902 这个出口在冷却
    state_path().write_text(json.dumps({
        "version": 1, "saved_at": 0.0,
        "slots": {"127.0.0.1:7902": {"cool_until": time.time() + 500, "bans": 1,
                                     "reason": "B0000"}},
    }), encoding="utf-8")

    # 顺序调换：7902 从第 2 位变成第 1 位
    swapped = ["http://127.0.0.1:7902", "http://127.0.0.1:7901"]
    p = ProxySlotPool(swapped)
    assert _cool_left(p, 1) > 400, "7902 现在在第 1 位，冷却应该跟着它"
    with p._cond:
        assert 2 not in p._cool_until, "7901 是干净的，不该被牵连"


def test_state_ignores_unknown_slots_and_bad_entries():
    """状态里有当前不存在的槽位 / 坏记录 → 忽略，不抛。"""
    import json

    from src.proxypool import state_path
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps({
        "version": 1, "saved_at": 0.0,
        "slots": {
            "127.0.0.1:7999": {"cool_until": time.time() + 500, "bans": 3},
            "127.0.0.1:7901": "这不是个 dict",
            "127.0.0.1:7902": {"cool_until": "不是数字", "bans": 1},
            "127.0.0.1:7903": {"cool_until": time.time() + 500, "bans": 2},
        },
    }), encoding="utf-8")

    logs = []
    p = ProxySlotPool(SLOTS, log=logs.append)
    assert _cool_left(p, 3) > 400, "能解析的记录要生效"
    with p._cond:
        assert set(p._cool_until) == {3}, f"坏记录不该进来：{p._cool_until}"
    assert any("当前不存在" in m for m in logs), f"未知槽位应告警：{logs}"
    assert any("格式不对" in m for m in logs), f"坏记录应告警：{logs}"


@pytest.mark.parametrize("garbage", [
    "{ 这不是 JSON",
    "[1, 2, 3]",                       # 顶层不是 dict
    '"just a string"',
    '{"slots": "不是 dict"}',
])
def test_corrupt_state_degrades_instead_of_raising(garbage):
    """坏掉的状态文件**不该让整批跑不起来** —— 只降级。"""
    from src.proxypool import state_path
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(garbage, encoding="utf-8")

    logs = []
    p = ProxySlotPool(SLOTS, log=logs.append)          # 不能抛
    assert p.size == 4
    assert p.stats()["cooling"] == {}
    assert p.stats()["bans"] == {}


def test_state_file_contains_no_credentials():
    """🔴 状态文件的键必须是 `host:port`，**不能**把槽位串整个写进去。

    槽位串可能是 `http://user:pass@host:port`。状态文件会被人直接打开看，
    也会被泄漏闸门扫 —— 不该因为"记个冷却时间"把账密落盘。
    """
    secret_slots = ["http://alice:s3cr3t@127.0.0.1:7901",
                    "http://bob:hunter2@127.0.0.1:7902"]
    p = ProxySlotPool(secret_slots)
    p.report_banned(p.acquire(timeout=1, accept=lambda i: i == 1), "B0000")

    from src.proxypool import state_path
    raw = state_path().read_text(encoding="utf-8")
    for secret in ("alice", "s3cr3t", "bob", "hunter2"):
        assert secret not in raw, f"状态文件里出现了凭据：{secret}"
    assert "127.0.0.1:7901" in raw, "但 host:port 要在，否则对不上槽位"


def test_leases_and_use_counts_are_not_persisted():
    """租约是**进程内**的东西；均衡计数从 0 重来无危害 —— 都不落盘。"""
    p = ProxySlotPool(SLOTS)
    lease = p.acquire(timeout=1)
    p.report_failed(lease, "timeout")          # 触发一次落盘

    slots = _state()["slots"]
    assert set(slots) == {"127.0.0.1:7901"}
    entry = slots["127.0.0.1:7901"]
    assert set(entry) == {"cool_until", "bans", "reason"}, \
        f"落盘的字段应只有这三个，实际 {sorted(entry)}"

    p2 = ProxySlotPool(SLOTS)
    assert p2.stats()["uses"] == {1: 0, 2: 0, 3: 0, 4: 0}
    assert p2.stats()["ip_held"] == {}, "不该把'被占着'也读回来"


def test_cooldown_uses_wall_clock():
    """🔴 冷却存的是**墙钟 epoch**，不是 monotonic。

    判据：值必须在 epoch 量级（> 1e9）。若哪天有人改回 `time.monotonic()`，
    `_cool_until` 会变成 3.7 这种小数字 —— 落盘再读回就永远是"已过期"，
    冷却静默失效。这条测试就是拦那个的。
    """
    p = ProxySlotPool(SLOTS, cooldown=100)
    p.report_banned(p.acquire(timeout=1, accept=lambda i: i == 1))

    raw = _state()["slots"]["127.0.0.1:7901"]["cool_until"]
    assert raw > 1e9, f"cool_until 看起来不是 epoch：{raw}"
    assert abs(raw - time.time() - 100) < 5, f"到期时刻不对：{raw}"


def test_expired_cooldown_is_pruned_on_save():
    """状态文件不该随运行次数无限增长 —— 写盘时清掉过期项。"""
    import json

    from src.proxypool import state_path
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps({
        "version": 1, "saved_at": 0.0,
        "slots": {"127.0.0.1:7904": {"cool_until": 1.0, "bans": 0}},
    }), encoding="utf-8")

    p = ProxySlotPool(SLOTS)
    p.report_banned(p.acquire(timeout=1, accept=lambda i: i == 1))
    assert set(_state()["slots"]) == {"127.0.0.1:7901"}, "过期且无封禁历史的记录应被清掉"


def test_save_leaves_no_temp_file():
    """写盘走"临时文件 + 原子替换"，不该留下 `.tmp` 残骸。"""
    from src.proxypool import state_path
    p = ProxySlotPool(SLOTS)
    p.report_banned(p.acquire(timeout=1, accept=lambda i: i == 1))

    leftovers = list(state_path().parent.glob("*.tmp"))
    assert leftovers == [], f"留下了临时文件：{leftovers}"
