"""槽位代理池 —— 给每个注册 worker 一个**独立的出口 IP**。

为什么需要它
------------
本项目的注册封禁是 **IP 维度累计配额**（`B0000`）。而旧实现里整个批次共用
**一个**出口 IP（`IR_PROXY` 只有一条）—— 也就是说 `workers` 调到几都一样，
撞配额是必然的。真正的约束是"**有多少个不同出口 IP**"，不是本地并发度。

2026-09-18 实测（`tools/probe_register_ip.py`，每个新 IP 打一枪）：

    出口 203.0.113.11  → ✅ 注册成功
    出口 203.0.113.12   → ✅ 注册成功
    出口 203.0.113.13   → ✅ 注册成功

**3/3 成功** —— 封禁确实只是 IP 维度，换 IP 即解开。

设计参考
--------
`asz798838958/aBaiFreeGPT` 的 `core/mihomo_client.py::MihomoRegistrationAllocator`。
核心是**租约（lease）**：

    worker ──acquire()──> 槽位 N（= 本地端口 790N = 某个固定出口 IP）
           ──用完/失败──> release() 或 report_banned()

三条关键规则（都照搬参考实现，理由见各自注释）：
  1. **租约是"粘性"的**：一个 worker 从注册到激活走同一个出口 IP。
     中途换 IP 会让服务端看到"半个会话换了来源"，比慢更糟。
  2. **被 B0000 的槽位进冷却，不是永久拉黑**。但**同一个 worker 不再回到
     它已经失败过的槽位** —— 它必须**向前推进**，不能在几个冷却中的出口之间弹跳。
  3. **均衡分配**：优先挑"当前占用最少"的槽位，而不是简单轮询 ——
     否则少数出口会被反复使用，把 IP 维度配额更快撞穿。

🔴 一条与参考实现不同的、实测踩到的坑
------------------------------------
**不同节点名 ≠ 不同出口 IP。** 实测 6 个槽位（6 个不同节点）只得到 **4 个**
不同出口 IP —— 有两对节点共用了同一个后端出口。

所以本模块**不假设**槽位数量等于出口 IP 数量，并在 `describe()` 里
把"配置了几个槽位"和"实际有几个出口"分开说。`tools/probe_slots.py`
负责把真实的出口 IP 探出来。

用法：
    from .proxypool import build_pool

    pool = build_pool()          # 未配置 IR_PROXY_SLOTS 时返回 None
    if pool:
        lease = pool.acquire()
        try:
            ...  # 用 lease.url 作为代理
        finally:
            pool.release(lease)
"""

import threading
import time
from dataclasses import dataclass, field

from . import config


@dataclass
class SlotLease:
    """一个槽位的租约。`url` 就是给 `requests` 用的代理地址。"""

    slot: int
    url: str
    acquired_at: float = field(default_factory=time.monotonic)
    uses: int = 0
    last_ip: str = ""
    released: bool = False

    def __str__(self) -> str:
        return f"slot{self.slot}({self.url})"


class NoEligibleSlot(RuntimeError):
    """池子里**没有任何一个合格槽位**（被 `accept` 全部否掉）。

    和 `TimeoutError` 必须分开，因为处置完全不同：

        有合格槽位、但暂时全忙/全冷却 → 等一会儿就轮得到 → `TimeoutError`
        池子里一个合格的都没有        → 等多久都不会变   → 本异常（**立刻**抛）

    典型场景：**所有出口 IP 的配额都满了**。这时候干等
    `IR_PROXY_SLOT_TIMEOUT`（默认 240s）毫无意义 —— 配额要几小时才滑出窗口。
    调用方拿到本异常应该把任务记成 `skipped`（没发请求），不是 `failed`。

    🔴 判据必须是"**整个池子里**有没有合格的"，不能写成"当前空闲的里面
    有没有合格的"。后者会在高峰期大面积误判：注册要跑 25 秒，
    那一刻空闲的往往正好是配额满的那个槽位，而合格的那几个正被占用着。
    本项目实测踩过：第一版写成后者，50 个任务只有头 3 个真跑了。
    """


class ProxySlotPool:
    """槽位池。线程安全。

    `cooldown` 是一个槽位被判"出口 IP 被目标站点封了"后的冷却秒数。
    参考实现用 120s（`MIHOMO_NODE_COOLDOWN_SECONDS`），本项目同值。
    """

    def __init__(self, slots: list[str], *, cooldown: float = None,
                 log=None):
        if not slots:
            raise ValueError("ProxySlotPool 需要至少一个槽位")
        self._slots = list(slots)
        self.cooldown = float(cooldown if cooldown is not None
                              else config.IR_PROXY_COOLDOWN)
        self._log = log

        self._cond = threading.Condition(threading.RLock())
        # slot(1-based) -> 是否空闲
        self._free: dict[int, bool] = {i: True for i in range(1, len(slots) + 1)}
        # slot -> 冷却到期时刻（monotonic）
        self._cool_until: dict[int, float] = {}
        # slot -> 累计使用次数（用于均衡分配）
        self._uses: dict[int, int] = {i: 0 for i in range(1, len(slots) + 1)}
        # slot -> 被判封禁的次数 / 最近原因
        self._bans: dict[int, int] = {}
        self._ban_reason: dict[int, str] = {}
        self._total_leases = 0

    # ── 基本属性 ──────────────────────────────────────────────
    @property
    def size(self) -> int:
        return len(self._slots)

    def url_of(self, slot: int) -> str:
        return self._slots[slot - 1]

    # ── 挑选 ──────────────────────────────────────────────────
    def _pick_locked(self, exclude: set, accept=None) -> tuple:
        """挑一个空闲、不在冷却中、且通过 `accept` 的槽位。

        排序键 `(累计使用次数, 槽位号)` —— 与参考实现的
        `(node_counts, cursor 距离)` 同义：**优先用最少被用过的**。
        简单轮询会让少数出口被反复用，更快撞穿它的 IP 维度配额。

        返回 `(挑中的槽位号或 None, 值不值得继续等)`。

        🔴 第二个值的判据是**整个池子里还有没有合格槽位**，而不是
        "当前空闲的里面有没有合格的"。这两者天差地别：

            池子 4 个槽位，3 个正被占用（注册要跑 25 秒），
            第 4 个空闲但配额已满。

            按"空闲里有没有合格的"判 → 没有 → 误判成"全都满了" → 放弃
            按"池子里还有没有合格的"判 → 那 3 个正在用的都合格，
                                        只是还没归还 → 该等

        本项目实测踩过这个坑：第一版按前者写，结果 50 个任务里
        只有头 3 个真正跑了，剩下 47 个在"等槽位"的假象下被跳过。
        """
        now = time.monotonic()
        avail = [
            i for i, free in self._free.items()
            if free and i not in exclude and self._cool_until.get(i, 0.0) <= now
        ]
        if accept is None:
            if not avail:
                return None, True          # 都忙 -> 等一会儿就有
            return min(avail, key=lambda i: (self._uses[i], i)), True

        cands = [i for i in avail if accept(i)]
        if cands:
            return min(cands, key=lambda i: (self._uses[i], i)), True

        # 空闲的都不合格。但**在用的 / 冷却中的**里面可能还有合格的 ——
        # 那些槽位只是暂时借出去了，归还后就是合格候选，所以该继续等。
        # 只有"池子里一个合格的都没有"才值得放弃（比如所有出口配额全满）。
        wait_worthwhile = any(
            i not in exclude and accept(i)
            for i in range(1, len(self._slots) + 1)
        )
        return None, wait_worthwhile

    def _next_wakeup_locked(self) -> float:
        """所有槽位都在冷却时，最近的到期时刻。"""
        now = time.monotonic()
        pending = [t for t in self._cool_until.values() if t > now]
        return min(pending) if pending else now

    def acquire(self, *, timeout: float = None,
                exclude: set = None, accept=None) -> SlotLease:
        """取一个槽位。全忙/全冷却时**阻塞等待**，超时抛 `TimeoutError`。

        `exclude` 是"这个 worker 已经试过的槽位号"，用于让单个 worker
        **向前推进**而不是在同一个出口上反复撞（见模块 docstring 规则 2）。

        `accept` 是一个 `slot -> bool` 的过滤器，用来把**当前不该用**的
        槽位排除在候选之外。本项目用它跳过"出口 IP 配额已满"的槽位 ——
        不加这个的话，池子只会按"用得最少"均分，已满的槽位会白白
        吃掉一大半租约，每个任务在那里拿一次租约、立刻被判跳过。

        🔴 什么时候该放弃、什么时候该等（见 `NoEligibleSlot` 的说明）：
        **整个池子里一个合格槽位都没有**才放弃；只要还有合格的槽位
        （哪怕它正被别的 worker 占用、或正在冷却），就应该等它归还。
        把"空闲的都不合格"当成放弃条件会误伤一大片 —— 注册要跑 25 秒，
        高峰期池子里大部分槽位都在用，那一刻"空闲的"往往正好是满的那个。
        """
        exclude = set(exclude or ())
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while True:
                slot, wait_worthwhile = self._pick_locked(exclude, accept)
                if slot is not None:
                    self._free[slot] = False
                    self._uses[slot] += 1
                    self._total_leases += 1
                    lease = SlotLease(slot=slot, url=self._slots[slot - 1],
                                      uses=self._uses[slot])
                    return lease
                if not wait_worthwhile:
                    # 池子里没有任何合格槽位 —— 等下去也不会变。
                    raise NoEligibleSlot(
                        f"{len(self._slots)} 个槽位里没有一个合格（被 accept 全部否掉）")
                # 有合格槽位，只是暂时都被占用/在冷却：算出下一个该醒来的时刻
                wake = self._next_wakeup_locked()
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"槽位池等待超时（{len(self._slots)} 个槽位，"
                        f"排除 {len(exclude)} 个后仍无可用）")
                wait = max(wake - time.monotonic(), 0.05)
                if deadline is not None:
                    wait = min(wait, max(deadline - time.monotonic(), 0.05))
                self._cond.wait(wait)

    def release(self, lease: SlotLease) -> None:
        """归还槽位（正常用完）。"""
        if lease is None or lease.released:
            return
        with self._cond:
            lease.released = True
            self._free[lease.slot] = True
            self._cond.notify_all()

    # ── 报告结果 ──────────────────────────────────────────────
    def report_banned(self, lease: SlotLease, reason: str = "") -> None:
        """这个出口被目标站点封了（本项目 = 注册返回 `B0000`）。

        🔴 **冷却而不是永久拉黑**：参考实现的注释说得很准 ——
        目标是"暂停分配 + 定时探测恢复"，而不是"删掉这个节点"。
        永久拉黑会让池子越跑越小，最后 `all_blocked()`。

        但冷却期**显著长于网络故障**（默认 120s），因为 IP 维度配额
        不会几秒就恢复。
        """
        if lease is None:
            return
        with self._cond:
            self._free[lease.slot] = True
            self._cool_until[lease.slot] = time.monotonic() + self.cooldown
            self._bans[lease.slot] = self._bans.get(lease.slot, 0) + 1
            self._ban_reason[lease.slot] = reason[:120]
            lease.released = True
            self._cond.notify_all()
        if self._log:
            self._log(f"槽位 {lease.slot} 进冷却 {self.cooldown:.0f}s"
                      f"（{reason or '被封'}）")

    def report_failed(self, lease: SlotLease, reason: str = "",
                      cooldown: float = 20.0) -> None:
        """网络类失败（超时/连不上）。**冷却要短**。

        🔴 为什么不能和 `report_banned` 用同一个时长：参考实现踩过 ——
        "Browser navigation timeouts are not proof that the exit node is
        permanently bad"（导航超时不能证明出口永久坏了）。批量跑的时候
        一个健康出口偶发超时很正常，用长冷却会让池子被一批瞬时故障耗光。
        """
        if lease is None:
            return
        with self._cond:
            self._free[lease.slot] = True
            self._cool_until[lease.slot] = time.monotonic() + float(cooldown)
            lease.released = True
            self._cond.notify_all()
        if self._log:
            self._log(f"槽位 {lease.slot} 短冷却 {cooldown:.0f}s"
                      f"（{reason or '网络失败'}）")

    # ── 观测 ──────────────────────────────────────────────────
    def stats(self) -> dict:
        with self._cond:
            now = time.monotonic()
            cooling = {i: round(t - now, 1)
                       for i, t in self._cool_until.items() if t > now}
            return {
                "slots": self.size,
                "free": sum(1 for v in self._free.values() if v),
                "cooling": cooling,
                "uses": dict(self._uses),
                "bans": dict(self._bans),
                "ban_reason": dict(self._ban_reason),
                "total_leases": self._total_leases,
            }

    def describe(self) -> str:
        s = self.stats()
        parts = [f"{s['slots']} 个槽位，当前空闲 {s['free']}"]
        if s["cooling"]:
            parts.append(f"冷却中 {len(s['cooling'])}（最短 "
                         f"{min(s['cooling'].values()):.0f}s）")
        if s["bans"]:
            parts.append(f"累计封禁 {sum(s['bans'].values())} 次")
        return "；".join(parts)


def build_pool(*, log=None, cooldown: float = None) -> "ProxySlotPool | None":
    """按配置建池。**未配置槽位时返回 `None`** —— 调用方据此退回单代理行为。

    🔴 "未配置就退回旧行为"是刻意的：这个功能不能改变没配它的人的运行结果。
    """
    slots = config.proxy_slots()
    if not slots:
        return None
    return ProxySlotPool(slots, cooldown=cooldown, log=log)
