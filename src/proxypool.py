"""槽位代理池 —— 给每个注册 worker 一个**独立的出口 IP**。

为什么需要它
------------
本项目的注册封禁是 **IP 维度累计配额**（`B0000`）。而旧实现里整个批次共用
**一个**出口 IP（`IR_PROXY` 只有一条）—— 也就是说 `workers` 调到几都一样，
撞配额是必然的。真正的约束是"**有多少个不同出口 IP**"，不是本地并发度。

2026-09-18 实测（`tools/probes/probe_register_ip.py`，每个新 IP 打一枪）：

    出口 203.0.113.11   → ✅ 注册成功
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
  4. **同出口 IP 互斥**（2026-09-19 新增，本项目自己的规则，参考实现没有）：
     同一时刻，**一个出口 IP 只允许一个租约在外**。理由见下面那节。

🔴 一条与参考实现不同的、实测踩到的坑（现已由规则 4 强制）
--------------------------------------------------------
**不同节点名 ≠ 不同出口 IP。** 实测 6 个槽位（6 个不同节点）只得到 **4 个**
不同出口 IP —— 有两对节点共用了同一个后端出口。

所以本模块**不假设**槽位数量等于出口 IP 数量，并在 `describe()` 里
把"配置了几个槽位"和"实际有几个出口"分开说。`tools/probes/probe_slots.py`
负责把真实的出口 IP 探出来。

🔴 为什么必须有规则 4（**分配单位与约束单位必须一致**）
----------------------------------------------------
本项目的封禁是 **IP 维度累计配额**。所以：

    约束的单位 = 出口 IP
    分配的單位 = 槽位

这两个单位**不相等**（实测 6 个槽位 → 4 个出口 IP），于是"按槽位分配"
会让**两个同 IP 的槽位被同时租出去** —— 服务端看到同一 IP 上 2 路并发，
撞穿速度翻倍。

⚠ 这个洞 `pipeline` 侧的 `accept=_slot_has_quota` **补不上**：它在
**拿租约之前**判断，两个线程可以同时通过"used=38/40 < 40"的检查。
拿到租约后那次复查（`pipeline.py` 里）也只查**自己这一个 slot**，
管不了"另一个 slot 是同一个 IP"。

⇒ 唯一能堵住它的地方是池子内部，因为只有池子同时看得见所有在外的租约。

⚠ 代价要说清楚：**有效并发度从"槽位数"降到"不同出口 IP 数"。**
在实测的 6 槽位 / 4 出口配置下，并发上限从 6 变 4。**这是对的** ——
那 2 个多出来的槽位本来就不提供额外配额，只是让撞穿更快。
`describe()` 会把这个数字打印出来，避免有人以为"配了 6 个就能跑 6 路"。

⚠ **没配 `IR_SLOT_EGRESS_IPS` 时不启用规则 4**（退回按槽位分配），
因为拿不到出口 IP 就无法分组。这种情况下 `describe()` 会显式告警 ——
"不知道出口 IP"和"出口 IP 互不相同"是两件完全不同的事，不能混为一谈。

状态持久化（2026-09-19 新增）
-----------------------------
**冷却到期时刻 + 封禁次数**会落到 `.workbuddy-ai/state/proxypool.json`
（`IR_PROXY_STATE` 可覆盖），建池时读回。

为什么必须持久化：`report_banned` 的冷却按 2 的幂退避（120s → 6h），
而服务端配额窗口是 **24h**。进程一退，退避就重置回 120s ——
每重跑一次批量，就在同一个被封的出口上重新撞一遍，**一天约 720 次**。

三条设计约束：

  1. **时间用 `time.time()`（墙钟），不是 `time.monotonic()`** ——
     monotonic 的原点是进程启动时刻，跨进程没有可比性。
     ⚠ `acquire()` 的 `deadline` / `wait` 仍是 monotonic（那是进程内时长）。
     两套钟不能混算，`_next_wakeup_locked()` 的 docstring 里记了踩法。
  2. **键用 `host:port`，不是位置号** —— `slots.txt` 增删一条会让位置号
     整体平移，冷却记录静默错配到别的出口头上（同 `SLOT_EGRESS_IPS` 那个坑）。
     也刻意不用完整 URL：它可能带账密。
  3. **读不出来就降级，不抛** —— 坏掉的状态文件不该让整批跑不起来。
     最坏后果只是退避从第一档重来。

不持久化 `_free` / `_ip_held`（租约是进程内的）与 `_uses`（均衡偏好，
从 0 重来无危害）。

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

import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, redact


def state_path() -> Path:
    """池子状态文件位置。`IR_PROXY_STATE` 可覆盖（测试隔离 / 多份状态并存）。

    为什么用**函数**而不是模块常量：`config` 在 import 时就把环境变量读完了，
    而自检要能在同一个进程里把状态重定向到临时文件 —— 见 `quota.state_path()`
    同样的理由。

    ⚠ 状态文件里存的是"哪些出口在冷却、被封过几次"，**不含账密**（键用的是
    `host:port`，见 `_state_key()`）。但它仍然属于运行态，落在
    `.workbuddy-ai/state/` 下（已被 `.gitignore` 排除）。
    """
    override = os.getenv("IR_PROXY_STATE")
    if override:
        return Path(override).expanduser()
    return (Path(__file__).resolve().parents[1]
            / ".workbuddy-ai" / "state" / "proxypool.json")


def _state_key(url: str) -> str:
    """槽位串 → 状态文件里的键：**只用 `host:port`**。

    🔴 为什么不用槽位号（`1`/`2`/…）：`slots.txt` 增删一条，后面所有槽位的
    位置号整体平移，冷却记录会**静默错配到别的出口头上** —— 与本项目
    `config.SLOT_EGRESS_IPS` 记的那个坑同形（实测已经发生过一次）。

    🔴 为什么不用完整 URL：槽位串可能带账密
    （`http://user:pass@host:port`，见 `_host_port` 的说明）。状态文件会被
    泄漏闸门扫，也会被人直接打开看 —— 不该因为"记个冷却时间"把账密写进去。
    `host:port` 在本项目里已经唯一（全是 `127.0.0.1` + 不同端口）。
    """
    host, port = _host_port(url)
    return f"{host}:{port}"


@dataclass
class SlotLease:
    """一个槽位的租约。`url` 就是给 `requests` 用的代理地址。"""

    slot: int
    url: str
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


class AllSlotsDead(RuntimeError):
    """配置了槽位，但**一个都连不上**（端口没有监听）。

    🔴 与 `NoEligibleSlot` 必须分开：那个是"池子在、只是这一刻没有合格的"，
    等一会儿可能就变了；本异常是"池子根本起不来"，等多久都不会变。

    🔴 为什么**不静默退回单代理**：槽位实例是独立的前台进程，很容易
    "配置还留着、进程已经没了"。那种状态下每条注册记录都会以
    **代理连接错误**收场，而"注册全失败"在本项目里最容易被误读成
    "换 IP 也不行 / 还在封" —— **结论完全错**，还会把人引向错误的排查方向。
    宁可启动时大声失败，也不要在跑批中途收获一屏看起来像风控的错误。
    """


def _host_port(url: str) -> tuple[str, int]:
    """从槽位串里取出 `(host, port)`。支持三种写法：

        http://user:pass@host:port    （带账密）
        http://127.0.0.1:7901
        127.0.0.1:7901                （裸 host:port）

    刻意手写而不用 `urlsplit`：`127.0.0.1:7901` 这种**没有 scheme** 的写法
    在 `urlsplit` 下会被当成 scheme（或落进 path），行为随输入形态而变。
    这里只需要 host 和 port，手写反而更确定。

    ⚠ 不支持 IPv6 字面量（`[::1]:7901`）。本项目的槽位一律是本机
    `127.0.0.1`，不引入这个复杂度；真要用 IPv6 得同时改这里和
    `config.slot_scope()`。
    """
    raw = (url or "").strip()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    if "@" in raw:
        raw = raw.rsplit("@", 1)[1]
    raw = raw.split("/", 1)[0]
    host, _, port = raw.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"无法从槽位串解析出 host:port：{url!r}")
    return host, int(port)


def check_slots_alive(slots: list[str], *, timeout: float = 0.5
                      ) -> tuple[list[str], list[str]]:
    """逐个做 TCP 连通检查，返回 `(活的, 死的)`。

    这就是 `IR_PROXY_PREFLIGHT` 承诺的那个检查（`config.py` 里定义了很久，
    一直零引用 —— 现在补上）。

    🔴 **为什么用 TCP 连而不是发一个真实请求**：这里要回答的问题只有
    "端口后面有没有进程"，TCP 就够，而且是毫秒级。用真实请求会
    （a）慢（每个槽位一次往返）、（b）把"代理活着但目标站被墙"和
    "代理根本没起来"混在一起 —— 后者才是这里要抓的。

    ⚠ **TCP 通 ≠ 出口可用**（出口 IP 会变、ACL 可能拦目标域名）。
    这个检查**不能替代** `tools/probes/probe_slots.py`，它只负责抓
    "槽位进程已经死了"这一种最粗、也最容易误判的故障。

    解析不出来的槽位串归入 `dead` —— 连地址都读不出来，谈不上可用。
    """
    alive: list[str] = []
    dead: list[str] = []
    for url in slots:
        try:
            host, port = _host_port(url)
        except ValueError:
            dead.append(url)
            continue
        try:
            with socket.create_connection((host, port), timeout=timeout):
                alive.append(url)
        except OSError:
            dead.append(url)
    return alive, dead


class ProxySlotPool:
    """槽位池。线程安全。

    `cooldown` 是一个槽位被判"出口 IP 被目标站点封了"后的**基础**冷却秒数。
    参考实现用 120s（`MIHOMO_NODE_COOLDOWN_SECONDS`），本项目同值。

    `cooldown_max` 是冷却退避的封顶（默认 6h）。**同一槽位反复被封时，
    冷却按 2 的幂递增**（120 → 240 → 480 …）直到封顶 —— 理由见
    `report_banned()`。

    `slot_ips` 是 `{槽位号: 出口 IP}` 映射。给了它就启用**同出口 IP 互斥**
    （见模块 docstring 规则 4）；给 `None` 则退回按槽位分配，并在
    `describe()` 里显式告警。
    """

    def __init__(self, slots: list[str], *, cooldown: float = None,
                 cooldown_max: float = None, slot_ips: dict = None,
                 log=None):
        if not slots:
            raise ValueError("ProxySlotPool 需要至少一个槽位")
        self._slots = list(slots)
        self.cooldown = float(cooldown if cooldown is not None
                              else config.IR_PROXY_COOLDOWN)
        self.cooldown_max = float(cooldown_max if cooldown_max is not None
                                  else config.IR_PROXY_COOLDOWN_MAX)
        self._log = log

        # 🔴 `slot_ips` 缺项 = 拿不到这个槽位的出口 IP ⇒ **整个池子退回按槽位分配**。
        #    不能只对缺项的那几个槽位跳过互斥：那样会出现"部分槽位参与互斥、
        #    部分不参与"，等于给同一个 IP 开了个后门，比完全不互斥更难排查。
        #    要么全都有映射，要么都不用 —— 二值，不做部分。
        self._slot_ip: dict[int, str] | None = None
        if slot_ips:
            self._slot_ip = {i: str(slot_ips[i])
                             for i in range(1, len(slots) + 1) if i in slot_ips}
            if len(self._slot_ip) != len(slots):
                if self._log:
                    self._log(
                        f"⚠ 只有 {len(self._slot_ip)}/{len(slots)} 个槽位登记了"
                        f"出口 IP ⇒ **整体退回按槽位分配**（不做部分互斥）。"
                        f"补齐 IR_SLOT_EGRESS_IPS 可启用同出口互斥。")
                self._slot_ip = None

        self._cond = threading.Condition(threading.RLock())
        # slot(1-based) -> 是否空闲
        self._free: dict[int, bool] = {i: True for i in range(1, len(slots) + 1)}
        # slot -> 冷却到期时刻（**墙钟 epoch**，不是 monotonic）
        # 🔴 必须用 `time.time()`：这份数据要**跨进程**读写（落盘再读回），
        #    而 `monotonic` 的原点是进程启动时刻，两个进程之间没有可比性 ——
        #    拿它落盘会得到"冷却到 3.7 秒"这种永远已过期的值，冷却静默失效。
        #    ⚠ 与它相对的是 `acquire()` 的 `deadline` / `wait`，那两个是**进程内**
        #    的时长，继续用 `time.monotonic()`（不受系统时钟调整影响）。
        #    两套钟不能混算 —— 见 `_next_wakeup_locked()` 的返回约定。
        self._cool_until: dict[int, float] = {}
        # slot -> 累计使用次数（用于均衡分配）
        self._uses: dict[int, int] = {i: 0 for i in range(1, len(slots) + 1)}
        # slot -> 被判封禁的次数 / 最近原因
        self._bans: dict[int, int] = {}
        self._ban_reason: dict[int, str] = {}
        # 🔀 出口 IP -> 当前在外的租约数。**同 IP 互斥的载体**（规则 4）。
        #    必须按"在外租约数"而不是"是否空闲"来记：只有这样才能在
        #    release/report_* 里对称地减回去，且对重复释放天然幂等。
        self._ip_held: dict[str, int] = {}
        self._total_leases = 0

        # 🔴 冷却与封禁次数**跨运行保留**（见模块 docstring 的"状态持久化"）。
        #    不保留的后果：封禁退避（120s → 6h）每次重跑都从 120s 重来，
        #    而服务端配额窗口是 24h —— 等于每轮批量都在重新撞一遍墙。
        self._load_state()

    # ── 基本属性 ──────────────────────────────────────────────
    @property
    def size(self) -> int:
        return len(self._slots)

    def url_of(self, slot: int) -> str:
        return self._slots[slot - 1]

    def _warn(self, msg: str) -> None:
        if self._log:
            self._log(msg)

    # ── 状态持久化 ────────────────────────────────────────────
    # 🔴 只持久化"跨运行仍然有效"的那部分：冷却到期时刻 + 封禁次数 + 原因。
    #
    #    不持久化 `_free` / `_ip_held`：租约是**进程内**的东西，把"某槽位被占着"
    #    写进磁盘只会让下一次运行以为它永远回不来。
    #    不持久化 `_uses`：均衡计数从 0 重来没有危害（它不是约束，只是偏好）。
    def _load_state(self) -> None:
        """从磁盘读回冷却与封禁。**任何异常都只降级、不抛。**

        降级而不是抛：一个坏掉的状态文件不该让整次批量跑不起来 ——
        那正是"清理一次数据反而连活都干不了"。真正的护栏在
        `report_banned` 的退避上（最坏情况是退避从 120s 重来）。
        """
        p = state_path()
        if not p.is_file():
            return
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError) as ex:
            self._warn(f"⚠ 池子状态文件读不出来（{p}）：{ex}"
                       f" —— 按「没有冷却记录」继续")
            return
        if not isinstance(raw, dict):
            self._warn(f"⚠ 池子状态文件格式不对（{p}）—— 已忽略")
            return

        # 🔴 按 `host:port` 匹配回当前槽位，**不是按位置号** —— 见 `_state_key()`。
        by_key = {_state_key(u): i for i, u in enumerate(self._slots, 1)}
        now = time.time()
        bad = unknown = 0
        # ⚠ `slots` 可能是任何东西（手改坏了、旧版本写的、被别的程序占了这个路径）。
        #    直接 `.items()` 会 AttributeError —— 而这条路的**全部意义**是
        #    "坏文件只降级不抛"，所以这里必须自己判类型，不能靠外层 try。
        raw_slots = raw.get("slots")
        if not isinstance(raw_slots, dict):
            self._warn(f"⚠ 池子状态文件里 `slots` 不是对象（{p}）—— 已忽略")
            return
        for key, ent in raw_slots.items():
            i = by_key.get(key)
            if i is None:
                unknown += 1
                continue
            if not isinstance(ent, dict):
                bad += 1
                continue
            try:
                cool_until = float(ent.get("cool_until") or 0.0)
                bans = int(ent.get("bans") or 0)
            except (TypeError, ValueError):
                bad += 1
                continue
            # 封禁次数**无论冷却是否过期都要留** —— 它决定下次的退避档位
            # （120 → 240 → … → 6h）。只留冷却不留次数，退避就永远是第一档。
            if bans:
                self._bans[i] = bans
            reason = ent.get("reason")
            if isinstance(reason, str) and reason:
                self._ban_reason[i] = reason
            if cool_until > now:
                self._cool_until[i] = cool_until

        if self._cool_until or self._bans:
            bits = []
            if self._cool_until:
                bits.append(f"{len(self._cool_until)} 个仍在冷却")
            if self._bans:
                bits.append(f"{len(self._bans)} 个有封禁历史")
            self._warn(f"♻ 读回池子状态：{'、'.join(bits)}（{p.name}）")
        if unknown:
            self._warn(f"⚠ 状态文件里有 {unknown} 条记录对应的槽位当前不存在 —— "
                       f"已忽略（`slots.txt` 改过？按 host:port 匹配就是为了防这个）")
        if bad:
            self._warn(f"⚠ 状态文件里有 {bad} 条记录格式不对 —— 已忽略")

    def _save_state_locked(self) -> None:
        """把冷却与封禁写盘（**调用方必须已持有 `self._cond`**）。

        顺带清掉已过期的冷却项 —— 状态文件不该随运行次数无限增长。
        过期但**有封禁历史**的槽位仍然写一条（`cool_until: 0`），
        否则退避档位会在冷却结束的那一刻丢失。

        ⚠ 文件 I/O 在锁内。可以接受：只有 `report_banned` / `report_failed`
        会调它，而这两件事在一次批量里是**低频**的（几次到几十次）。
        """
        now = time.time()
        self._cool_until = {i: t for i, t in self._cool_until.items() if t > now}

        slots: dict = {}
        for i in sorted(set(self._cool_until) | set(self._bans)):
            n = self._bans.get(i, 0)
            t = self._cool_until.get(i, 0.0)
            if not n and t <= now:
                continue
            slots[_state_key(self._slots[i - 1])] = {
                "cool_until": round(t, 3),
                "bans": n,
                "reason": self._ban_reason.get(i, ""),
            }

        p = state_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再原子替换：批量跑一半被 Ctrl-C，不该留下半个 JSON
            # （下次读会降级成"没有状态"，冷却全丢 —— 静默的那种）。
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"version": 1, "saved_at": round(now, 3), "slots": slots},
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
            os.replace(tmp, p)
        except OSError as ex:
            self._warn(f"⚠ 池子状态写盘失败（{p}）：{ex} —— 本次运行不受影响，"
                       f"但冷却不会跨运行保留")

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
        now = time.time()          # 与 `_cool_until` 同钟（墙钟 epoch），见 __init__
        avail = [
            i for i, free in self._free.items()
            if free and i not in exclude and self._cool_until.get(i, 0.0) <= now
        ]
        # 🔀 同出口 IP 互斥（模块 docstring 规则 4）：这个出口已经有租约在外
        #    就跳过。**必须放在这里（候选集过滤），不能放进 accept** ——
        #    accept 是调用方传进来的"这个槽位该不该用"的语义判断
        #    （比如"出口配额满了"），而 IP 互斥是**池子内部的一致性约束**，
        #    两者混在一起会让 `NoEligibleSlot` 的判据失准：配额满要立刻放弃，
        #    IP 被占则应该等它归还。
        if self._slot_ip is not None:
            avail = [i for i in avail
                     if self._ip_held.get(self._slot_ip[i], 0) == 0]
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
        #
        # 🔴 这个判据**刻意不排除"IP 被占用"的槽位**：IP 占用是**瞬时**的
        #    （持有者迟早 release），所以那些槽位仍然是合格的，只是还没回来。
        #    若在这里把"IP 被占"也算成不合格，一个 6 槽位 / 4 出口的池子
        #    在 4 个出口全满时会误判成"池子里没有合格槽位" → 直接抛
        #    `NoEligibleSlot` → 把本该等待的任务全部记成 skipped。
        #    这正是 `NoEligibleSlot` docstring 里记的那个坑的同一形态。
        wait_worthwhile = any(
            i not in exclude and accept(i)
            for i in range(1, len(self._slots) + 1)
        )
        return None, wait_worthwhile

    def _next_wakeup_locked(self) -> float:
        """所有槽位都在冷却时，最近的到期时刻。

        🔴 返回的是**墙钟 epoch**（与 `_cool_until` 同钟），不是时长。
        调用方 `acquire()` 必须用 `wake - time.time()` 去算还要等多久 ——
        写成 `time.monotonic()` 会得到 `1.7e9 - 3000 ≈ 1.7e9` 秒的等待，
        整个池子从此再也醒不过来（而且不报错，只是永远超时）。
        """
        now = time.time()
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
                    # 记这个出口"多了一个在外租约" —— 与 _release_locked 成对。
                    # 两者必须在同一个锁内，否则两个线程可能同时看到 ip_held==0
                    # 而各拿一个租约（那正是规则 4 要堵的洞）。
                    if self._slot_ip is not None:
                        ip = self._slot_ip[slot]
                        self._ip_held[ip] = self._ip_held.get(ip, 0) + 1
                    return SlotLease(slot=slot, url=self._slots[slot - 1])
                if not wait_worthwhile:
                    # 池子里没有任何合格槽位 —— 等下去也不会变。
                    raise NoEligibleSlot(
                        f"{len(self._slots)} 个槽位里没有一个合格（被 accept 全部否掉）")
                # 有合格槽位，只是暂时都被占用/在冷却：算出下一个该醒来的时刻
                # ⚠ `wake` 是**墙钟 epoch**，所以这里减 `time.time()`；
                #    `deadline` 是**进程内**的单调钟，减 `time.monotonic()`。
                #    两套钟各减各的，不要合并 —— 混算不会报错，只会算出
                #    一个荒谬的等待时长（见 `_next_wakeup_locked` 的说明）。
                wake = self._next_wakeup_locked()
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"槽位池等待超时（{len(self._slots)} 个槽位，"
                        f"排除 {len(exclude)} 个后仍无可用）")
                wait = max(wake - time.time(), 0.05)
                if deadline is not None:
                    wait = min(wait, max(deadline - time.monotonic(), 0.05))
                self._cond.wait(wait)

    def _release_locked(self, lease: SlotLease) -> None:
        """把租约还回去（**调用方必须已持有 `self._cond`**）。幂等。

        🔴 三条归还路径（`release` / `report_banned` / `report_failed`）
        共用这一个实现，所以 `_free` 与 `_ip_held` 不可能出现
        "一个减了、另一个没减"的漂移。分别写三遍是这类计数 bug 的温床。
        """
        if lease is None or lease.released:
            return
        lease.released = True
        self._free[lease.slot] = True
        if self._slot_ip is not None:
            ip = self._slot_ip[lease.slot]
            left = self._ip_held.get(ip, 0) - 1
            if left > 0:
                self._ip_held[ip] = left
            else:
                # 归零就删键（避免字典随出口数无限增长）。
                # 🔴 负值必须夹到 0 而不是留着：`_pick_locked` 的判据是
                #    `== 0`，一旦出现负数那个出口就**永久锁死**，
                #    而且症状是"池子莫名少一个出口"，极难排查。
                self._ip_held.pop(ip, None)
        self._cond.notify_all()

    def release(self, lease: SlotLease) -> None:
        """归还槽位（正常用完）。"""
        with self._cond:
            self._release_locked(lease)

    # ── 报告结果 ──────────────────────────────────────────────
    def report_banned(self, lease: SlotLease, reason: str = "") -> None:
        """这个出口被目标站点封了（本项目 = 注册返回 `B0000`）。

        🔴 **冷却而不是永久拉黑**：参考实现的注释说得很准 ——
        目标是"暂停分配 + 定时探测恢复"，而不是"删掉这个节点"。
        永久拉黑会让池子越跑越小，最后 `all_blocked()`。

        但冷却期**显著长于网络故障**（基础 120s），因为 IP 维度配额
        不会几秒就恢复。

        🔴 **同一槽位反复被封时，冷却按 2 的幂退避**（2026-09-19 新增）。
        修的是一个**参数失配**：基础冷却 120s，而服务端配额窗口是
        **24h**（`config.REG_QUOTA_WINDOW_H`）—— 差 720 倍。而
        `quota.record()` **只在注册成功时调用**，封禁**不写**配额台账，
        于是 120s 后本地计数没涨、`pipeline` 侧的 `accept` 照样放行，
        这个已被封的出口被重新租出去，再打一次注定失败的请求 ——
        **每 120s 一次，一天约 720 次**，每次都在加深封禁。

        退避把"一天 720 次"压到"一天 6 次左右"：
        120 → 240 → 480 → 960 → 1920 → 3840 → 7680 → 15360 → 封顶 6h。

        ⚠ 这是**治标**。治本是让 `report_banned` 把封禁写进配额台账，
        使 `accept` 在配额窗口内真正拦得住（见 `docs/refactor-plan-2026-09-19.md`
        §2.2 的 B1）。退避先落地是因为它只要 3 行、且不碰配额模块。
        """
        if lease is None:
            return
        with self._cond:
            n = self._bans.get(lease.slot, 0)        # 本次**之前**已封几次
            # min(n, 16) 只是防 `2 ** n` 在 n 很大时溢出成天文数字；
            # 实际上 base=120 / cap=21600 时 n=8 就已封顶，走不到那里。
            cool = min(self.cooldown * (2 ** min(n, 16)), self.cooldown_max)
            self._release_locked(lease)
            self._cool_until[lease.slot] = time.time() + cool
            self._bans[lease.slot] = n + 1
            self._ban_reason[lease.slot] = reason[:120]
            self._save_state_locked()
        if self._log:
            self._log(f"槽位 {lease.slot} 进冷却 {cool:.0f}s"
                      f"（第 {n + 1} 次被封；{reason or '被封'}）")

    def report_failed(self, lease: SlotLease, reason: str = "",
                      cooldown: float = 20.0) -> None:
        """网络类失败（超时/连不上）。**冷却要短**。

        🔴 为什么不能和 `report_banned` 用同一个时长：参考实现踩过 ——
        "Browser navigation timeouts are not proof that the exit node is
        permanently bad"（导航超时不能证明出口永久坏了）。批量跑的时候
        一个健康出口偶发超时很正常，用长冷却会让池子被一批瞬时故障耗光。

        ⚠ 这里**刻意不退避**：网络抖动是随机的，不是"这个出口越来越坏"的
        证据。只有 `report_banned`（目标站明确说"你被封了"）才退避。
        """
        if lease is None:
            return
        with self._cond:
            self._release_locked(lease)
            self._cool_until[lease.slot] = time.time() + float(cooldown)
            self._save_state_locked()
        if self._log:
            self._log(f"槽位 {lease.slot} 短冷却 {cooldown:.0f}s"
                      f"（{reason or '网络失败'}）")

    # ── 观测 ──────────────────────────────────────────────────
    def egress_of(self, slot: int) -> str:
        """这个槽位的出口 IP；未登记映射时返回空串。

        给日志 / 告警用。⚠ 空串的含义是"**不知道**"，不是"没有出口" ——
        调用方不要把它当成一个值去比较。
        """
        if self._slot_ip is None:
            return ""
        return self._slot_ip.get(slot, "")

    @property
    def distinct_egress(self) -> int | None:
        """不同出口 IP 的个数 = **真实的并发上限**。未登记映射时返回 `None`。

        🔴 这才是"能并发跑几个"的答案，`size`（槽位数）不是。
        实测 6 个槽位只对应 4 个出口 ⇒ 并发上限是 4。
        """
        if self._slot_ip is None:
            return None
        return len(set(self._slot_ip.values()))

    def stats(self) -> dict:
        with self._cond:
            now = time.time()      # 与 `_cool_until` 同钟
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
                "slot_ip": dict(self._slot_ip or {}),
                "distinct_egress": self.distinct_egress,
                "ip_held": dict(self._ip_held),
            }

    def describe(self) -> str:
        s = self.stats()
        parts = [f"{s['slots']} 个槽位，当前空闲 {s['free']}"]
        if s["distinct_egress"] is not None:
            # 🔴 槽位数与出口数**必须分开说**，而且要说清后者才是并发上限 ——
            #    否则"配了 6 个槽位"会被读成"能跑 6 路"。
            parts[0] = (f"{s['slots']} 个槽位（{s['distinct_egress']} 个不同出口 IP，"
                        f"并发上限 {s['distinct_egress']}），当前空闲 {s['free']}")
        else:
            # 🔴 没登记映射时**主动告警**，不能默不作声 ——
            #    "不知道出口 IP"和"出口 IP 互不相同"是两件事，
            #    沉默会让人以为互斥已经生效。
            parts.append("⚠ 未登记出口 IP 映射 ⇒ 同出口互斥未启用，"
                         "同 IP 的槽位可能被同时租出")
        if s["cooling"]:
            parts.append(f"冷却中 {len(s['cooling'])}（最短 "
                         f"{min(s['cooling'].values()):.0f}s）")
        if s["bans"]:
            parts.append(f"累计封禁 {sum(s['bans'].values())} 次")
        return "；".join(parts)


def _resolve_slot_ips(slots: list[str], log) -> "dict | None":
    """把槽位清单映射成 `{槽位号: 出口 IP}`；**任何一个解析不出来就整体返回 None**。

    🔴 刻意"全有或全无"，不做部分映射 —— 见 `ProxySlotPool.__init__` 里
    "部分互斥比不互斥更难排查"的说明。
    """
    out: dict = {}
    for i, url in enumerate(slots, 1):
        try:
            out[i] = config.slot_scope(url)
        except ValueError as ex:
            if log:
                # 只取异常首行：`slot_scope` 的报错是多行的补全指引，
                # 在启动日志里展开会把"池子建不起来"这件正事淹掉。
                log(f"⚠ 出口 IP 映射不全（槽位 {i}："
                    f"{str(ex).splitlines()[0]}）⇒ **同出口互斥整体不启用**")
            return None
    return out


def build_pool(*, log=None, cooldown: float = None,
               preflight: bool = None) -> "ProxySlotPool | None":
    """按配置建池。**未配置槽位时返回 `None`** —— 调用方据此退回单代理行为。

    🔴 "未配置就退回旧行为"是刻意的：这个功能不能改变没配它的人的运行结果。

    `preflight`：起飞前做一次槽位端口连通检查（默认取
    `config.IR_PROXY_PREFLIGHT`，出厂为开）。传 `False` 可跳过 ——
    自检和离线测试必须能跳过它，否则会在没有真实槽位的环境里误报。

    抛 `AllSlotsDead`：配了槽位但**一个端口都没监听**。刻意不静默退回单代理，
    理由见 `AllSlotsDead` 的 docstring。
    """
    slots = config.proxy_slots()
    if not slots:
        return None

    if preflight is None:
        preflight = config.IR_PROXY_PREFLIGHT
    if preflight:
        alive, dead = check_slots_alive(slots)
        if dead:
            # ⚠ 必须脱敏：槽位串可能是 `http://user:pass@host:port`。
            names = "、".join(redact.redact_url(u) for u in dead)
            if log:
                log(f"⚠ 端口预检：{len(dead)}/{len(slots)} 个槽位连不上"
                    f"（端口无监听），已剔除 → {names}")
        if not alive:
            names = "\n  ".join(redact.redact_url(u) for u in slots)
            raise AllSlotsDead(
                f"配置了 {len(slots)} 个槽位，但没有一个端口在监听：\n  {names}\n"
                f"  ⇒ 槽位实例（mihomo）没起来。先跑：\n"
                f"     python tools/ops/proxypool_ctl.py status\n"
                f"     python tools/ops/proxypool_ctl.py start\n"
                f"  ⚠ 不要在这个状态下直接跑批量 —— 每条记录都会以"
                f"**代理连接错误**收场，看起来像「换 IP 也不行」，结论是错的。\n"
                f"  （确定要跳过检查：IR_PROXY_PREFLIGHT=0）")
        slots = alive

    slot_ips = _resolve_slot_ips(slots, log)
    if slot_ips is not None and log:
        distinct = len(set(slot_ips.values()))
        if distinct < len(slots):
            # 🔴 这是**关键提示**：并发上限由出口数决定，不是槽位数。
            #    实测过 6 槽位 / 4 出口，不提示的话人会按 6 去配 workers。
            log(f"🔀 槽位 {len(slots)} 个，但只有 {distinct} 个不同出口 IP "
                f"⇒ 并发上限 {distinct}（同出口互斥已启用）")
    return ProxySlotPool(slots, cooldown=cooldown, slot_ips=slot_ips, log=log)
