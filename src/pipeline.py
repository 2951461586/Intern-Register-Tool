"""端到端编排 —— 注册 → 激活 → 登录取 JWT → 领额度 → 建 API Key。

阶段划分与依赖：
  Stage 1+2 注册激活  纯 HTTP（SSO + 临时邮箱）  ← 无需人机验证，~10s
  Stage 3   登录      浏览器（阿里云验证码）      ← 唯一必须用浏览器的环节，~20s
  Stage 4   建 Key    纯 HTTP（discovery）        ← 用 Stage 3 拿到的 JWT，~3s
  Stage 5   校验      纯 HTTP（推理网关）         ← 非致命

────────────────────────────────────────────────────────────────────
并发设计（为什么是"两段式流水线"而不是"线程池跑全链"）
────────────────────────────────────────────────────────────────────
两个阶段的资源特性完全不同：

  Stage 1+2  纯 HTTP、快（~10s）、**可以高并发**（4~8 路无压力）
  Stage 3    浏览器、慢（~20s）、**并发受风控限制**（同一出口 IP 下不宜过多）

如果用"N 个线程各跑完整链路"，两者会被绑成同一个并发度：浏览器并发度
被注册阶段的并发度牵着走。而浏览器恰恰是稀缺资源。

所以拆成生产者-消费者：

    ┌─ 生产者池（并发 4）─┐        ┌─ 消费者池（并发 = workers）─┐
    │ 建邮箱→注册→收信→激活 │ ────▶ │ 登录→领额度→建Key→校验       │
    └───────────────────┘  队列   └──────────────────────────┘

注册阶段可以跑在前面把账号"备好"，浏览器侧按自己的节奏消费。
注册（10s）被隐藏进登录（20s）里，理论吞吐提升约 1.4 倍；
再叠加 workers 路并行，总提升 ≈ workers × 1.4。

⚠ workers > 1 意味着**同一出口 IP 上并发登录**。阿里云按 IP + 指纹 + 频率
  打分，实测 workers=2 可用（见 README「并发实测」）。但这是需要实测确认的
  参数，不是可以随便调大的旋钮 —— 风控收紧时请回退到 workers=1。
"""

import json
import queue
import random
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field

from . import config, quota
from .discovery import DiscoveryClient
from .proxypool import NoEligibleSlot, build_pool
from .sso import SSOClient
from .tempmail import TempMailClient

# 注册阶段的并发度。纯 HTTP，可以给得比浏览器侧高。
# 注意：并发度 ≠ 注册速率 —— 速率由下面的 REG_MIN_INTERVAL 闸门控制。
REG_CONCURRENCY = 4

# 两次 `register/byEmail` 之间的最小间隔（秒）。
# 🔴 实测 `register/byEmail` 有写操作限流：4 路**并发**（同时到达）时 3 路立刻被
#    429 拒绝，而只读的 `personal/username/check` 8 路并发也不限流。
#    → 限流挂在**写操作 + 突发**上，不是笼统的 IP 速率限制。
#
# 边界实测（`tools/probes/probe_reg_interval.py`，绕开退避重试打裸请求，
# 降序试探 + 见 429 即停）：
#
#     间隔    成功  429   平均耗时
#     2.5s     4     0     0.89s  ✅
#     2.0s     4     0     0.81s  ✅
#     1.5s     4     0     0.80s  ✅
#     1.0s     4     0     0.80s  ✅  ← 四档全清，未探到悬崖
#
# 取 1.2s = 实测干净的 1.0s + 20% 余量。
#
# ⚠ 但别指望它带来吞吐提升：**注册根本不是瓶颈**。
#   workers=2 时浏览器侧的消耗速率是 2/18s ≈ 0.11 账号/秒，
#   而 1.2s 闸门给出 0.83 账号/秒 —— 快 7 倍。
#   收窄它的真实收益只有两点：① 首屏"账号就绪"更快，减少 worker 冷启动空转
#   （workers=2/4 账号时约省 1.5s）；② 队列不会堆深，便于把 workers 调大。
REG_MIN_INTERVAL = 1.2

# 注册接口在累计配额触顶时返回的 msgCode。
# 🔴 别把它当成"瞬时速率"错误 —— 调 REG_MIN_INTERVAL 无效（见 src/quota.py）。
QUOTA_MSG_CODE = "B0000"

# 连续见到几个 `B0000` 就认定配额触顶、停止后续投递。
# 取 2 而不是 1：单次 B0000 有可能是服务端抖动，等第二个确认再停，
# 代价只有一次请求；而误停的代价是整批账号被跳过。
QUOTA_STREAK_STOP = 2


# ── 错误分类（`AccountRecord.error_kind`）────────────────────────────
#
# 为什么要有这个字段：在这之前，"这个失败是不是出口被封"靠**在 error 文本里
# 搜 `B0000`** 判断。文本匹配有两个毛病：
#
#   ① 我们自己拼的**守卫文案**里也引用了 `B0000`
#      （`"quota guard: 已确认 B0000（累计配额触顶），未发注册请求"`）。
#      于是"一个请求都没发的中止"会被读成"服务端确认的封禁"。
#      `QuotaGovernor.note_result` 已经为此单独加了一句排除，注释写着
#      「不排除就会被当成新证据重复计数」—— 同样的地雷在 `settle_lease`
#      里还埋着（见那里的说明）。
#   ② 改一句文案就可能悄悄改掉行为，而 diff 里看不出来。
#
# 结构化之后，"服务端到底返回了什么"由**打标点**决定，读点只认字段。
# 打标点与读点分离：打标只在**真的拿到服务端响应**的地方做（`stage_register`），
# 读点统一走 `error_kind_of()`。
ERR_NONE = ""                # 没有错误
ERR_QUOTA = "quota"          # 服务端返回 `B0000` —— **出口维度**累计配额触顶
ERR_QUOTA_GUARD = "quota_guard"   # 本地守卫主动中止，**一个请求都没发**
ERR_REJECTED = "rejected"    # 服务端明确拒绝了这次注册（非配额）
ERR_NETWORK = "network"      # 网络 / 超时 / HTTP 层
ERR_BROWSER = "browser"      # 浏览器阶段（登录 / 建 key）


def is_quota_block(text: str) -> bool:
    """判断**服务端返回文本**是否表示注册累计配额触顶。

    ⚠ 这是**文本级**工具，只该用在"手里只有一段服务端响应文本"的地方
    （如 `probe_register_ip.py` 判 `res.msg_code + res.msg`）。
    **判断一条 `AccountRecord` 是不是被封，请用 `error_kind_of()`** ——
    拿 `rec.error` 来喂这个函数会把我们自己拼的守卫文案也算进去。
    """
    return QUOTA_MSG_CODE in (text or "")


def _rec_field(rec, name: str) -> str:
    """从 `AccountRecord` **或**从 `results.json` 读回来的 dict 里取一个字符串字段。

    两种形态都要支持：流水线里是对象，而 `run.py` 走台账那条路时是 dict
    （`json.loads(r.to_json())`）。非字符串一律当空串 —— 这几个字段只可能是字符串。
    """
    v = rec.get(name, "") if isinstance(rec, dict) else getattr(rec, name, "")
    return v if isinstance(v, str) else ""


def error_kind_of(rec) -> str:
    """读一条记录的错误类别 —— **唯一读点**。接受 `AccountRecord` 或 dict。

    规则：**字段非空即权威**。只有字段为空时才退化成文本匹配 ——
    那是给"手工构造 / 从旧 `results.json` 读回来的记录"兜底的
    （`error_kind` 是 2026-09-19 才加的字段，老记录里没有）。

    🔴 `status == "skipped"` 永远返回"无配额证据"。这不是启发式，是逻辑：
    中止意味着**一个请求都没发**，没有请求就不可能有服务端响应。
    这条也顺带修掉了 `settle_lease` 里的地雷 —— 见那里的 docstring。
    """
    kind = _rec_field(rec, "error_kind")
    if kind:
        return kind
    if _rec_field(rec, "status") == "skipped":
        return ERR_NONE
    if is_quota_block(_rec_field(rec, "error")):
        return ERR_QUOTA
    return ERR_NONE


class _QuotaAbort(RuntimeError):
    """配额保护主动中止 —— **未发出任何注册请求**。

    与"注册失败"必须区分开：失败是"试过了，不行"；中止是"没试，因为
    前面的证据已经足够"。混在一起会让人误以为服务端又拒了一批。
    """


class _RateLimiter:
    """跨线程的最小间隔闸门：保证两次调用之间至少间隔 min_interval 秒。"""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def __call__(self):
        with self._lock:
            now = time.time()
            if now < self._next:
                time.sleep(self._next - now)
                now = self._next
            self._next = now + self.min_interval


class QuotaGovernor:
    """配额决策的**唯一**去处 —— 把原先散在 `run_batch` 三处的判据收进一个对象。

    为什么要收拢
    ------------
    改造前这三处判据散在 `run_batch` 里，而且**必须按运行模式分叉**：

    | 时机 | 非池模式 | 槽位池模式 |
    |---|---|---|
    | 开跑前 | 用全局 scope 裁计划量 | **跳过**（全局计数是老出口的，拦了会误判） |
    | 拿租约前 | — | 按**该出口**的 scope 预筛 `accept` |
    | 拿到租约后 | — | 按该出口的 scope 再查一遍 |
    | 运行中 | 连续 `QUOTA_STREAK_STOP` 个 `B0000` 就置位"停" | **不置位**（`B0000` 是出口维度，不是整批触顶） |

    分叉本身是对的（理由见 `allow()` 与 `note_result()`），但散在三处之后，
    看任何一处都推不出全局行为 —— 尤其"运行中哨兵只在非池模式生效"这条，
    改造前是靠 `_note_register_result` 开头一句 `if pool is not None: … return`
    隐式表达的，还得专门写注释解释为什么。

    ⚠ **`ignore=False` 时判据一字未改**。等价性由 `tests/test_quota_governor.py`
    的差分测试保证 —— 那里内嵌了改造前从 `run_batch` 抄下来的原始内联逻辑，
    逐输入对比两者结论，而不是靠"读代码觉得一样"。

    🔴 `ignore=True`（`--ignore-quota`）必须覆盖**全部三个**检查点，
    一处漏掉整个开关就是摆设：
        `allow()`       —— 原逻辑就有
        `check_slot()`  —— 2026-09-20 补（原逻辑漏，实测 50 批次 0/50 全 skipped）
        `claim_slot()`  —— 2026-09-20 补（同上）
    漏在槽位模式下的表现最恶劣：`check_slot` 是 `accept` 谓词，它一路返回
    `False` 会让 `acquire()` 抛 `NoEligibleSlot`，报出来的话术是
    "所有出口配额均已满（4 个槽位里没有一个合格）" —— 看着像**池子满了**，
    实际是**开关没生效**。这条误导性文案本身就是排查成本，所以现在
    三个检查点齐了，并且各有独立的差分/行为用例钉住（`IGNORE_*` 用例）。
    ⚠ 唯一**不**受 `ignore` 影响的是 `check_slot()` 里"端口没登记出口 IP"
    那一支 —— 它拦的不是"额度用完了"，而是"不知道该把额度记到谁头上"，
    理由写在那个分支上。

    ⚠ 这个类**有状态**（运行中哨兵要跨线程累计），但状态只有一个计数 + 一个
    `Event`。不要往这里加别的东西 —— 它只该回答"现在能不能发请求"。
    """

    def __init__(self, *, pool=None, ignore: bool = False, log=print):
        self.pool = pool
        self.ignore = ignore
        # 运行级日志（只有 `allow()` 用）。producer 级的日志带 `[i/n]` 前缀，
        # 由调用方逐次传入 —— 见 `claim_slot()`。
        self.log = log
        self._hit = threading.Event()
        self._lock = threading.Lock()
        self._streak = 0

    # ── 开跑前 ────────────────────────────────────────────────────
    def allow(self, count: int) -> int:
        """返回**实际允许投递**的账号数（可能被裁小）。"""
        if self.ignore:
            return count
        if self.pool is not None:
            # 🔀 槽位模式下**跳过全局守卫**：本地计数是**按出口 IP** 记的，
            #    而这里的历史计数（scope 为空）属于老的单代理出口。拿它来拦
            #    槽位批次会得到错误的结论（实测：53/40 "超额"，但 6 个新出口
            #    其实一个都没用过）。改由每个 producer 按自己的 scope 单独检查。
            self.log(f"ℹ 槽位模式：跳过全局配额守卫（本地计数 "
                     f"{quota.status().describe()} 是**老出口**的，与新槽位无关）——\n"
                     f"  改按槽位分别计数，每个出口各自独立。")
            return count
        # 🔴 为什么必须在**投递前**拦，而不是等失败再停：`B0000` 是累计量限制，
        #    撞上之后**连单账号都注册不了**，继续投递只是在加深封禁、并制造
        #    一堆假失败记录。宁可少跑几个，也不要撞墙。详见 src/quota.py。
        # allow_partial：还有余量就放行（这里自己裁计划量），余量为 0 才抛。
        st = quota.check_or_raise(planned=count, allow_partial=True)
        if st.used + count > st.limit:
            keep = st.remaining
            self.log(f"⚠ 本地配额保护：计划注册 {count} 个，但{st.describe()} "
                     f"—— 本次只跑 {keep} 个。\n"
                     f"  想全跑：调大 IR_REG_QUOTA_MAX，或加 --ignore-quota"
                     f"（先确认服务端确实已恢复）。")
            return keep
        return count

    # ── 拿租约 ────────────────────────────────────────────────────
    def check_slot(self, index: int) -> bool:
        """`accept` 谓词：第 `index` 个出口**现在**还有额度吗（拿租约**之前**）。

        🔴 不加这一步的话，池子只会按"用得最少"均分租约，已满的槽位会白白
        吃掉一大半 —— 实测 50 个任务只成 16 个，而池子里明明还躺着 44 个
        额度没用（全在没被分到的槽位上）。
        """
        if self.pool is None:
            return True
        try:
            # ⚠ `pool.url_of()` 必须留在 try 里（见 test_check_slot_rejects_unmapped_port）。
            scope = config.slot_scope(self.pool.url_of(index))
        except ValueError:
            # 端口没登记出口 IP：不敢用，否则配额会被静默记错地方。
            # 🔴 这一支**刻意不受 `--ignore-quota` 影响**。它拦的不是"额度用完了"
            #    而是"不知道该把额度记到谁头上" —— 放行等于让某个真实出口
            #    悄悄超过服务端上限（然后**真**被封），而 `--ignore-quota` 的
            #    语义只是"别信本地那个保守计数"，不是"允许账目错乱"。
            #    两种失败的可见度也完全不同：超限被封有 `B0000` 明示，
            #    记错 scope 是**静默**的，事后查不出来。
            return False
        if self.ignore:
            # `--ignore-quota`：本地计数是保守估计，用户已确认服务端恢复了。
            # 🔴 少了这一句，槽位模式下 `accept` 会被全部否掉 →
            #    `acquire()` 抛 NoEligibleSlot → 整批 0 个，且报错话术
            #    看起来像"池子满了"（2026-09-20 实测 50 批次 0/50）。
            return True
        return not quota.status(scope=scope).exhausted

    def claim_slot(self, scope: str, log=print) -> str:
        """拿到租约后**再查一遍**。返回跳过原因，`""` = 放行。

        🔴 为什么必须复查：`check_slot()` 是在**拿租约之前**判的，从拿到租约
        到真正发请求之间，可能有别的线程把最后一点额度用掉了。

        `log` 由调用方传入 —— producer 级的日志带 `[i/n]` 前缀，
        收进 `self.log` 会把所有 producer 的输出混成一路。
        """
        if self.ignore:
            # 与 `check_slot()` 同一把开关。只补 `check_slot` 是不够的：
            # 那一处管"能不能拿到租约"，这一处管"拿到之后放不放行"，
            # 漏掉这里会变成"租约照拿、请求不发"，白占一个出口。
            return ""
        st = quota.status(scope=scope)
        if not st.exhausted:
            return ""
        log(f"跳过（出口 {scope} 配额保护：{st.describe()}）")
        return (f"quota guard: 出口 {scope} 本地计数已满"
                f"（{st.describe()}），未发请求")

    # ── 运行中 ────────────────────────────────────────────────────
    def note_result(self, ok: bool, rec) -> None:
        """注册结果反馈 —— 运行中哨兵的**唯一**喂食口。"""
        with self._lock:
            if self.pool is not None:
                # 🔀 槽位模式下 `B0000` 是**出口维度**的信号，不是"整批触顶"。
                #    旧逻辑在这里会把整批后续任务标 skipped —— 而实测 3 个不同
                #    出口都能注册成功，那样做等于**把还有余量的出口一起停掉**。
                #    自我保护改由槽位冷却承担：被封的出口 120s 内不再分配；
                #    若所有出口都被封，`acquire()` 会阻塞到超时，那些任务按失败
                #    记账 —— 效果等价于"停下来"，但不会误伤干净的出口。
                if ok:
                    self._streak = 0
                return
            if ok:
                self._streak = 0
            elif rec.status == "skipped":
                # 主动中止：没发请求，既不是配额证据也不是恢复证据。
                # ⚠ 必须显式排除 —— 它的 error 文本里也含 B0000（是引用，
                #   不是服务端返回），不排除就会被当成新证据重复计数。
                # （`error_kind_of()` 现在也会把 skipped 归成"无证据"，
                #  但这一句保留：它表达的是**语义**，不是补文本匹配的洞。）
                pass
            elif error_kind_of(rec) == ERR_QUOTA:
                self._streak += 1
                if self._streak >= QUOTA_STREAK_STOP:
                    self._hit.set()
            # 其他失败：不动计数。
            # （若在这里清零，一个无关的失败就能把已确认的配额信号抹掉。）

    def hit(self) -> bool:
        """运行中已确认配额触顶。**只在非池模式下可能为真**（见 `note_result`）。"""
        return self._hit.is_set()

    def streak(self) -> int:
        """当前连续 `B0000` 计数（诊断与测试用）。"""
        return self._streak


def settle_lease(pool, lease, ok: bool, rec) -> None:
    """按注册结果归还槽位租约。

    🔴 三种结果必须分开处理 —— 冷却时长差 6 倍（120s vs 20s）：
        出口被封（B0000） → report_banned  长冷却，配额不会几秒就恢复
        网络类失败        → report_failed  短冷却，偶发超时很正常
        正常/主动中止     → release        立刻还回去
    混成一种的后果：要么把健康出口按长冷却晾 2 分钟（吞吐塌），
    要么把被封出口当偶发故障 20s 后重投（继续撞墙）。

    🔴 判据走 `error_kind_of()`，**不能**改成 `is_quota_block(rec.error)`：
    我们自己拼的守卫文案里含 `B0000`（是引用，不是服务端返回），文本匹配会把
    "一个请求都没发的主动中止"读成"服务端确认的封禁"，于是一个**干净出口**
    被白晾 120s。`QuotaGovernor.note_result` 早就为此加过排除（见那里的注释），
    这里是同一个坑的另一半 —— 用结构化字段从根上消掉：`ERR_QUOTA` 只在真的
    拿到 `B0000` 响应时才写。

    ⚠ 刻意放在**模块级**而不是 `run_batch` 里的闭包：闭包没法单独测，而这条
    判据的等价性是靠差分测试证明的（`tests/test_quota_governor.py`）。
    """
    if pool is None or lease is None:
        return
    if error_kind_of(rec) == ERR_QUOTA:
        pool.report_banned(lease, f"{QUOTA_MSG_CODE} @ {rec.email}")
    elif ok:
        pool.release(lease)
    elif rec.status == "skipped":
        # 主动中止：一个请求都没发，出口是干净的，别浪费一次冷却。
        pool.release(lease)
    else:
        pool.report_failed(lease, rec.error[:80])


def gen_username(prefix: str = "lz") -> str:
    return prefix + "".join(random.choices(string.digits, k=6))


def gen_password(length: int = 12) -> str:
    """生成符合规则（8-20 位，含两类以上字符）的密码。"""
    body = "".join(random.choices(string.ascii_letters + string.digits, k=length - 3))
    return "Lz#" + body


@dataclass
class AccountRecord:
    email: str = ""
    username: str = ""
    password: str = ""
    sso_uid: str = ""
    jwt: str = ""
    api_key: str = ""
    key_id: str = ""
    credits: str = ""
    status: str = "init"
    error: str = ""
    # 错误的**结构化类别**（取值见 ERR_* 常量）。空 = 没有错误。
    # 🔴 为什么不直接看 `error` 文本：我们自己拼的守卫文案里也含 `B0000`，
    #    文本匹配分不清"服务端返回的"和"我们引用的"。理由与取值见 ERR_* 那段。
    # 读它请用 `error_kind_of()`，不要直接读字段 —— 那里有老记录的兜底。
    error_kind: str = ""
    stages: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    created_at: str = ""
    # 这个账号注册时用的出口槽位（`slot3(http://127.0.0.1:7903)`）。
    # 空 = 没启用槽位池（走的全局 IR_PROXY 或直连）。
    # 🔴 记它是为了事后能回答"被封的到底是哪个出口" —— 光看 `B0000`
    #    不知道是 IP 维度还是别的维度，必须能把它和出口对应起来。
    proxy_slot: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


# ────────────────────────────────────────────────────────────────
# Stage 1+2：建邮箱 → 注册 → 收信激活（纯 HTTP）
# ────────────────────────────────────────────────────────────────
def stage_register(mail: TempMailClient, sso: SSOClient, rec: AccountRecord,
                   *, mail_domain: str = None, log=print, gate=None,
                   should_stop=None, quota_scope: str = "") -> bool:
    """gate: 可选的限速闸门（callable）。在真正调用 register/byEmail 前触发，
    用于把注册速率钉死在安全区间内（见 REG_MIN_INTERVAL）。

    should_stop: 可选的无参 callable，返回 True 表示**已确认配额触顶**，
    此时**不发注册请求**、直接把记录标成 skipped。

    quota_scope: 出口作用域（槽位池模式下传 `"slot3"`）。服务端配额按出口 IP
    记，所以本地计数也必须按出口分开 —— 见 `quota.status()` 的说明。
    空串 = 单出口模式，与加这个参数之前的行为完全一致。

    🔴 为什么这个检查必须放在 `gate()` **之后**：`gate` 是全局串行点
    （所有 producer 在这里排队），放在它后面才保证"看到前一个的失败结果"。
    放在 `producer` 开头只能挡住**尚未启动**的任务 —— 而 `ThreadPoolExecutor`
    会把 `min(count, reg_conc)` 个任务**同时**启动，它们会在任何失败发生之前
    一起通过检查（实测：count=4 / reg_conc=4 时 4 个 B0000、0 个跳过）。
    换句话说，开头那道检查只管"批量剩余部分"，管不了"并发窗口内已起飞的部分"。

    🔬 子阶段计时（`rec.timings["register_detail"]`）：
    这一阶段整体 7~13s，但各环节的理论耗时之和只有 ~4s。不拆开就不知道
    多出来的时间在哪 —— 是闸门在等、还是收信在等、还是接口本身慢。
    """
    t0 = time.time()
    sub = {}

    def mark(k: str, t_from: float):
        sub[k] = round((time.time() - t_from) * 1000)

    try:
        t = time.time()
        emails = mail.create_mailbox(domain=mail_domain, count=1)
        rec.email = emails[0]
        mark("mailbox", t)
        log(f"mailbox: {rec.email}")

        # 不再调 check_email：邮箱是 Worker 刚建的，不可能已注册。
        # 万一撞上（地址被回收复用），register 本身会报错，不影响正确性。
        t = time.time()
        username = gen_username()
        for _ in range(6):
            if sso.check_username(username):
                break
            username = gen_username()
        rec.username = username
        rec.password = gen_password()
        mark("username", t)

        t = time.time()
        if gate:
            gate()          # 限速：两次 register/byEmail 之间至少 REG_MIN_INTERVAL
        mark("gate_wait", t)

        # 最后一道闸：过了限速闸门（全局串行点）再看一眼配额信号。
        # 放这里才看得到前序请求的结果 —— 理由见本函数 docstring。
        if should_stop is not None and should_stop():
            raise _QuotaAbort(
                f"已确认 {QUOTA_MSG_CODE}（累计配额触顶），未发注册请求")

        t = time.time()
        reg = sso.register(rec.username, rec.email, rec.password)
        mark("register_call", t)
        if not reg.ok:
            detail = f"{reg.msg_code} {reg.msg}".strip()
            # 打标而不是在这里处理：stage_register 是单账号函数，
            # "停止整批"的决策属于 run_batch（它才看得到全局节奏）。
            if is_quota_block(detail):
                rec.stages["quota_blocked"] = QUOTA_MSG_CODE
                rec.error_kind = ERR_QUOTA
            else:
                rec.error_kind = ERR_REJECTED
            raise RuntimeError(f"register failed: {detail}")
        rec.sso_uid = reg.sso_uid
        rec.stages["register"] = "ok"
        # 只有**成功**才计入本地配额 —— 失败的不占额度，记了会虚高。
        quota.record(rec.email, scope=quota_scope)
        log(f"registered: uid={rec.sso_uid} user={rec.username}")
    except _QuotaAbort as ex:
        # 主动中止 ≠ 失败：一个请求都没发，不该混进失败统计里。
        rec.status = "skipped"
        rec.error = f"quota guard: {ex}"
        rec.error_kind = ERR_QUOTA_GUARD
        rec.timings["register"] = round((time.time() - t0) * 1000)
        rec.timings["register_detail"] = sub
        log("跳过（配额保护）")
        return False
    except Exception as ex:
        rec.status = "failed"
        rec.error = f"register: {ex}"
        # ⚠ `or` 不能省：上面 `if not reg.ok` 那条路已经在抛之前打好标了，
        #    这里若无条件覆盖，`ERR_QUOTA` 会被冲成 `ERR_NETWORK`。
        #    （它的异常类型是 `RuntimeError`，会被这个 `except Exception` 接到。）
        rec.error_kind = rec.error_kind or ERR_NETWORK
        rec.timings["register"] = round((time.time() - t0) * 1000)
        rec.timings["register_detail"] = sub
        return False

    t_reg_done = time.time()
    try:
        t = time.time()
        m = mail.wait_for_mail(rec.email)
        mark("mail_wait", t)
        # 🔬 记下这次收信打了几次 `/admin/all`（含 5xx 重试）。
        # 那个接口每次请求都是一次**全表 `SELECT *`**（部署版本忽略 email
        # 过滤），背后是 D1 —— 所以这两个数就是"注册一个账号要花掉多少
        # D1 读取"的凭据。D1 读限额被打满时，靠它对账，不靠猜。
        sub["mail_polls"] = getattr(mail, "last_polls", 0)
        sub["mail_5xx"] = getattr(mail, "last_http_errors", 0)
        if not m:
            # 把"邮件没到"和"邮箱服务读不出来"分开报 —— 前者是等，后者是坏，
            # 修法完全不同（见 tempmail.wait_for_mail 的 5xx 说明）。
            why = getattr(mail, "last_error", "") or "activation mail not received within timeout"
            rec.error_kind = ERR_REJECTED
            raise RuntimeError(why)

        # 🔬 把 `mail_wait` 拆成"邮件真正到达"与"轮询开销"两段。
        # 不拆就是在猜 —— 7.87s 到底是 SMTP/Worker 慢，还是我们的轮询太钝，
        # 两者的修法完全不同（前者无解，后者调 limit/interval 即可）。
        # `received_at` 是**毫秒** unix 时间戳（实测 1789449135216）。
        arrival = m.received_at / 1000.0 if m.received_at > 1e11 else float(m.received_at)
        sub["arrival_delay_ms"] = round((arrival - t_reg_done) * 1000)
        sub["poll_overhead_ms"] = sub["mail_wait"] - sub["arrival_delay_ms"]

        link = m.find_link("active", "activat", "verif", "confirm")
        if not link:
            rec.error_kind = ERR_REJECTED
            raise RuntimeError("activation link not found in mail")

        t = time.time()
        if not sso.activate_from_url(link):
            rec.error_kind = ERR_REJECTED
            raise RuntimeError("activate returned success=false")
        mark("activate_call", t)
        rec.stages["activate"] = "ok"
        log("activated")
    except Exception as ex:
        rec.status = "failed"
        rec.error = f"activate: {ex}"
        # 上面三处 `raise` 都是"服务端行为不符合预期"，已在抛出点打好标；
        # 这里只兜底 `wait_for_mail` 自己抛的网络异常（`or` 不能省，理由同上）。
        rec.error_kind = rec.error_kind or ERR_NETWORK
        rec.timings["register"] = round((time.time() - t0) * 1000)
        rec.timings["register_detail"] = sub
        return False

    rec.timings["register"] = round((time.time() - t0) * 1000)
    rec.timings["register_detail"] = sub
    return True


# ────────────────────────────────────────────────────────────────
# Stage 3+4+5：登录 → 领额度 → 建 Key → 校验
# ────────────────────────────────────────────────────────────────
def stage_login_key(rec: AccountRecord, *, session=None, headless: bool = False,
                    key_name: str = "default", verbose: bool = True,
                    screenshot_prefix: str = None, log=print,
                    verify: bool = True) -> bool:
    """Stage 3+4+5。

    session: 若传入 `BrowserSession`，复用它（批量场景）；否则自建浏览器。
    verify:  是否在本阶段内做 key 可用性校验。
             🔴 批量场景应传 False —— 新建 key 在网关侧有 ~10s 传播延迟，
             在关键路径上等它等于每个账号白等 ~7s。改由 `verify_keys()`
             在流水线末尾统一校验，那时传播早已完成，几乎瞬时。
    """
    t0 = time.time()
    try:
        if session is not None:
            res = session.login(rec.email, rec.password,
                                screenshot_prefix=screenshot_prefix,
                                verbose=verbose)
        else:
            from .browser import login as browser_login

            res = browser_login(rec.email, rec.password, headless=headless,
                                screenshot_prefix=screenshot_prefix, verbose=verbose)
        if not res.ok:
            raise RuntimeError(res.reason or "login failed")
        rec.jwt = res.jwt
        rec.stages["login"] = "ok"
        # 记下验证码走的通路（A=TRACELESS 自过/零点击，B=降级点击）。
        # 批量排查时必须带着这个维度看耗时，否则会把通路切换误读成性能回归。
        cs = res.captcha_stage or {}
        if cs.get("path"):
            rec.stages["captcha_path"] = cs["path"]
        rec.timings["login"] = round((time.time() - t0) * 1000)
        rec.timings["login_detail"] = res.timings
        log(f"jwt acquired ({len(rec.jwt)} chars), cookies={list(res.cookies.keys())}")
    except Exception as ex:
        rec.status = "failed"
        rec.error = f"login: {ex}"
        rec.error_kind = ERR_BROWSER
        rec.timings["login"] = round((time.time() - t0) * 1000)
        return False

    t1 = time.time()
    try:
        # 关键：`GET/POST /tokenplan/v1/keys` 比同组其他接口校验更严，
        # 仅带 Authorization 会返回 -10002 request is not authenticated，
        # 必须同时带上浏览器会话的 Cookie。
        dc = DiscoveryClient(jwt=rec.jwt, cookies=res.cookies)

        info = dc.get_user_info()
        log(f"user: {info.get('sso_username')} <{info.get('sso_email')}>")

        status = dc.free_grant_status()
        if not status.get("has_received"):
            dc.claim_free_grant()
            log("free grant claimed")

        bal = dc.balance()
        rec.credits = str(bal.get("available_credits", ""))
        log(f"credits={rec.credits} rpm={bal.get('rpm_limit')}")

        api_key = dc.create_key(key_name)
        rec.api_key = api_key.key
        rec.key_id = api_key.id
        rec.stages["key"] = "ok"
        log(f"API KEY = {api_key.key}")

        # 非致命校验：key 已建出来，但确认它真的能过推理网关。
        # 批量场景下这一步被推迟到流水线末尾（verify=False），见 verify_keys()。
        if verify:
            try:
                from . import apikey as _ak

                if _ak.wait_until_active(api_key.key):
                    models = _ak.list_models(api_key.key)
                    rec.stages["verify"] = f"ok({len(models)} models)"
                    log(f"key verified, {len(models)} models available")
                else:
                    rec.stages["verify"] = "not active yet"
                    log("⚠ key 已建出但网关侧尚未生效（传播延迟），稍后重试即可")
            except Exception as ex:
                rec.stages["verify"] = f"failed: {str(ex)[:120]}"
                log(f"⚠ key verify failed: {str(ex)[:120]}")

        rec.timings["key"] = round((time.time() - t1) * 1000)
        rec.status = "success"
        return True
    except Exception as ex:
        rec.status = "failed"
        rec.error = f"key: {ex}"
        rec.error_kind = ERR_BROWSER
        rec.timings["key"] = round((time.time() - t1) * 1000)
        return False


# ────────────────────────────────────────────────────────────────
# 批量校验 key（流水线末尾统一做）
# ────────────────────────────────────────────────────────────────
def verify_keys(records: list, *, verbose: bool = True, log=print) -> None:
    """统一校验已建出的 API Key 能否调用推理网关。

    为什么单独放最后，而不是塞在 `stage_login_key` 的关键路径里：
      新建 key 在网关侧有 ~10s 传播延迟。若每个账号建完就地等，等于给
      每个账号白加 ~7s（实测「建Key」阶段 7.3~8.6s，其中 ~6s 是干等）。
      挪到流水线末尾时，第一个账号的 key 早就过了传播期，校验几乎瞬时。
    并发校验，非致命 —— 失败只写进 stages，不影响账号本身的成功状态。
    """
    targets = [r for r in records if r.api_key and r.status == "success"]
    if not targets:
        return
    from . import apikey as _ak

    def one(rec: AccountRecord) -> tuple:
        try:
            if _ak.wait_until_active(rec.api_key, attempts=3, delay=3.0):
                models = _ak.list_models(rec.api_key)
                return rec, f"ok({len(models)} models)"
            return rec, "not active yet"
        except Exception as ex:
            return rec, f"failed: {str(ex)[:100]}"

    with ThreadPoolExecutor(max_workers=min(len(targets), 8)) as ex:
        for f in as_completed([ex.submit(one, r) for r in targets]):
            rec, verdict = f.result()
            rec.stages["verify"] = verdict
            if verbose:
                log(f"key verify {rec.email}: {verdict}")


# ────────────────────────────────────────────────────────────────
# 单账号
# ────────────────────────────────────────────────────────────────
def run_one(*, headless: bool = False, key_name: str = "default",
            mail_domain: str = None, verbose: bool = True,
            screenshot_prefix: str = None) -> AccountRecord:
    """完整跑通一个账号（顺序执行，便于调试）。"""
    rec = AccountRecord(created_at=time.strftime("%Y-%m-%d %H:%M:%S"))

    def log(msg: str):
        if verbose:
            print(f"    {msg}", flush=True)

    t0 = time.time()
    mail, sso = TempMailClient(), SSOClient()
    if not stage_register(mail, sso, rec, mail_domain=mail_domain, log=log):
        return rec

    if verbose:
        print("    launching browser to pass captcha ...", flush=True)
    stage_login_key(rec, headless=headless, key_name=key_name, verbose=verbose,
                    screenshot_prefix=screenshot_prefix, log=log)
    rec.timings["total"] = round((time.time() - t0) * 1000)
    return rec


# ────────────────────────────────────────────────────────────────
# 批量：两段式流水线
# ────────────────────────────────────────────────────────────────
def run_batch(*, count: int, workers: int = 2, headless: bool = False,
              key_name: str = "default", mail_domain: str = None,
              verbose: bool = True, screenshot_prefix: str = None,
              reg_concurrency: int = None,
              ignore_quota: bool = False) -> list[AccountRecord]:
    """批量注册，注册阶段与浏览器阶段流水线并行。

    Args:
        count: 账号数量
        workers: 浏览器并发数。**这是受风控约束的参数**，不要盲目调大。
                 workers=1 等价于顺序执行（最保守）。
        headless: 无头模式
        reg_concurrency: 注册阶段**线程并发度**，默认 4。注意这不等于注册
                 **速率** —— 速率由 REG_MIN_INTERVAL 闸门控制（写操作限流）。
                 并发度给高是为了让建邮箱、收信轮询并行。
        ignore_quota: 跳过本地配额保护。仅当**确信**服务端配额已恢复、
                 而本地计数还没滑出窗口时才用（计数是保守估计，不是权威值）。

    🔀 槽位代理池：配了 `IR_PROXY_SLOTS`（或 `IR_PROXY_SLOTS_FILE`）就自动启用，
    每个 producer 独占一个出口 IP。这是**唯一**能绕开 IP 维度累计配额的办法
    （封禁背景见 `src/proxypool.py` 的模块 docstring）。没配时行为与加这个
    功能之前**完全一致**（`build_pool()` 返回 None）。

    Returns:
        与提交顺序一致的 AccountRecord 列表。注意：配额保护可能把 count
        裁小、或把部分记录标成 `status="skipped"` —— 所以返回长度未必等于
        传入的 count，调用方应以返回列表为准，不要假设下标齐全。
    """
    if workers < 1:
        raise ValueError("workers must be >= 1")

    # ── 槽位代理池（可选）──────────────────────────────────────
    # 🔴 必须在配额守卫**之前**建：守卫要看"是不是槽位模式"才能决定
    #    该不该按单出口的计数拦人（见下面那段注释）。
    #    未配置槽位时 build_pool() 返回 None —— 走 `lease = None` 那条路，
    #    行为与加这个功能之前**完全一致**。
    pool = build_pool(log=lambda m: print(f"[pool] {m}", flush=True))
    if pool is not None:
        print(f"🔀 槽位代理池已启用：{pool.describe()}\n"
              f"   每个注册 worker 独占一个出口 IP。"
              f"（{QUOTA_MSG_CODE} 只封那个出口，不再中断整批）", flush=True)

    # ── 配额守卫（三处决策都收在 QuotaGovernor 里）──────────────────
    # 开跑前拦 + 拿租约前预筛 + 拿到租约后复查 + 运行中哨兵。
    # 🔴 为什么必须在**投递前**拦，而不是等失败再停：`B0000` 是累计量限制，
    #    撞上之后**连单账号都注册不了**，继续投递只是在加深封禁、并制造一堆
    #    假失败记录。宁可少跑几个，也不要撞墙。
    #    分模式的分叉理由见 `QuotaGovernor` 的类 docstring。
    # 🔴 开关的可见性警告刻意放在**调用点**而不是 `allow()` 里面：`allow()` 的
    #    输出被差分测试逐字对比（`_ref_allow` 是改造前的原样抄写，ignore 时
    #    不打印），往那里加一行会当场把测试打红 —— 而测试是对的，不该为了
    #    一行提示去改"历史判据"。
    if ignore_quota:
        print("⚠ --ignore-quota 已开启：本地配额守卫**全部三个检查点**都跳过"
              "（开跑前裁剪 / 拿租约前预筛 / 拿到租约后复查）。\n"
              f"  本地计数只是保守估计，跳过它意味着**完全依赖服务端**："
              f"真触顶会直接吃 {QUOTA_MSG_CODE}。\n"
              "  那是出口维度的封禁，不会自己恢复 —— 请自行确认窗口已滑出。",
              flush=True)
    gov = QuotaGovernor(pool=pool, ignore=ignore_quota,
                        log=lambda m: print(m, flush=True))
    count = gov.allow(count)

    reg_conc = reg_concurrency or min(count, REG_CONCURRENCY)
    results: list = [None] * count
    q: queue.Queue = queue.Queue()
    print_lock = threading.Lock()

    def make_log(idx: int):
        prefix = f"[{idx + 1}/{count}]"

        def log(msg: str):
            if verbose:
                with print_lock:
                    print(f"{prefix} {msg}", flush=True)

        return log

    # ── 消费者：每个 worker 一个浏览器会话，复用进程 ──────────
    def consumer_worker(wid: int):
        from .browser import BrowserSession

        try:
            with BrowserSession(headless=headless) as sess:
                if verbose:
                    with print_lock:
                        print(f"[worker {wid + 1}] browser ready "
                              f"({sess.launch_ms}ms)", flush=True)
                while True:
                    item = q.get()
                    try:
                        if item is None:
                            return
                        idx, rec = item
                        # init = 还没被处理过；failed / skipped 都无需登录。
                        if rec.status != "init":
                            continue
                        with print_lock:
                            print(f"[{idx + 1}/{count}] "
                                  f"login+key: {rec.email}", flush=True)
                        stage_login_key(
                            rec, session=sess, key_name=key_name,
                            verbose=False, log=make_log(idx), verify=False,
                            screenshot_prefix=(f"{screenshot_prefix}_{idx + 1}"
                                               if screenshot_prefix else None),
                        )
                    finally:
                        q.task_done()
        except Exception as ex:
            # 浏览器起不来时不能让整批卡死：把队列里剩下的都标记失败
            with print_lock:
                print(f"[worker {wid + 1}] 会话异常: {ex}", flush=True)
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    return
                try:
                    if item is None:
                        return
                    _, rec = item
                    rec.status = "failed"
                    rec.error = f"browser worker failed: {str(ex)[:120]}"
                    rec.error_kind = ERR_BROWSER
                finally:
                    q.task_done()

    consumers = [threading.Thread(target=consumer_worker, args=(i,), daemon=True)
                 for i in range(workers)]
    for t in consumers:
        t.start()

    # ── 生产者：并发注册 + 激活，完成一个就投递一个 ────────────
    # 注册速率由闸门钉死（register/byEmail 有写操作限流，见 REG_MIN_INTERVAL）
    reg_gate = _RateLimiter(REG_MIN_INTERVAL)

    # 运行中配额保护（fail-fast）。
    # 开跑前的检查只能看到"历史累计"，看不到"本次跑着跑着就触顶"的情况
    # （本地上限是估计值，服务端真实阈值可能更低）。所以运行中还要有个哨兵：
    # 连续见到 QUOTA_STREAK_STOP 个 B0000 就置位，后续 producer 直接跳过 ——
    # 既不浪费请求，也不再加深封禁。
    # 🔴 哨兵状态与判据都在 `gov` 里（`note_result()` 喂食、`hit()` 读）。

    def producer(idx: int):
        rec = AccountRecord(created_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        results[idx] = rec
        log = make_log(idx)
        # 廉价预筛：挡住**尚未启动**的任务。真正的最后一道闸在 stage_register
        # 内部、限速闸门之后（见那里的注释）—— 因为本函数开头这次检查对
        # "并发窗口内已起飞"的任务无效。
        if gov.hit():
            rec.status = "skipped"
            rec.error = (f"quota guard: 前序账号已触发 {QUOTA_MSG_CODE}，"
                         f"跳过投递（未发请求）")
            rec.error_kind = ERR_QUOTA_GUARD
            log("跳过（配额保护）")
            q.put((idx, rec))
            return
        ok = False
        lease = None
        scope = ""
        try:
            if pool is not None:
                # 独占一个出口 IP。全池都在冷却时阻塞等待，超时抛 TimeoutError。
                #
                # 🔴 `accept=gov.check_slot` 把"出口配额已满"的槽位**提前排除在
                #    候选之外**（判据在 `QuotaGovernor.check_slot`，理由也写在那里）。
                try:
                    lease = pool.acquire(timeout=config.IR_PROXY_SLOT_TIMEOUT,
                                         accept=gov.check_slot)
                except NoEligibleSlot as ex:
                    # 有空闲槽位，但它们的出口配额全满了。
                    # 配额要几小时才滑出窗口 ⇒ 等下去毫无意义，直接记"跳过"。
                    rec.status = "skipped"
                    rec.error = (f"quota guard: 所有出口配额均已满"
                                 f"（{ex}），未发请求")
                    rec.error_kind = ERR_QUOTA_GUARD
                    log(f"跳过（所有出口配额已满：{ex}）")
                    return
                except TimeoutError as ex:
                    # 等不到槽位 ≠ 这个账号注册失败，但也绝不能假装成功。
                    rec.status = "failed"
                    rec.error = f"register: {ex}"
                    rec.error_kind = ERR_NETWORK
                    log(f"⏳ 等不到空闲槽位（{pool.describe()}）")
                    return
                # 🔴 scope 取**出口 IP**，不是槽位位置号 —— 位置号会随
                #    `slots.txt` 增删条目整体错位，把配额记到别的 IP 头上
                #    （本项目实测已发生过，见 config.SLOT_EGRESS_IPS 的说明）。
                scope = config.slot_scope(lease.url)
                rec.proxy_slot = str(lease)
                log(f"出口槽位 {lease}（配额记在 {scope} 名下）")
                # 这个出口自己的配额（不是全局的 —— 见 quota.status 的说明）。
                # 复查的判据在 `QuotaGovernor.claim_slot`（理由也写在那里）。
                why = gov.claim_slot(scope, log)
                if why:
                    rec.status = "skipped"
                    rec.error = why
                    rec.error_kind = ERR_QUOTA_GUARD
                    return
            mail = TempMailClient()
            # 🔴 proxy 必须传**具体值**给这个 client，不能改全局 `IR_PROXY` ——
            #    多个 producer 同时改全局会互相踩（见 config.apply_proxy）。
            sso = SSOClient(proxy=(lease.url if lease else None))
            ok = stage_register(mail, sso, rec, mail_domain=mail_domain, log=log,
                                gate=reg_gate, should_stop=gov.hit,
                                quota_scope=scope)
        except Exception as ex:
            rec.status = "failed"
            rec.error = f"register: {ex}"
            rec.error_kind = rec.error_kind or ERR_NETWORK
        finally:
            settle_lease(pool, lease, ok, rec)
            gov.note_result(ok, rec)
            q.put((idx, rec))

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=reg_conc) as ex:
        list(ex.map(producer, range(count)))

    if gov.hit():
        skipped = sum(1 for r in results
                      if r is not None and r.status == "skipped")
        # 只有真跳过了才报 —— skipped==0 说明配额信号是在**最后几个任务
        # 已经起飞之后**才确认的，此时失败列表已经把故事讲完了，再报一次是噪声。
        if skipped:
            print(f"⚠ 运行中确认配额触顶（连续 {QUOTA_STREAK_STOP} 个 "
                  f"{QUOTA_MSG_CODE}）→ 跳过后续 {skipped} 个（未发请求）。\n"
                  f"  {quota.status().describe()}", flush=True)

    # 投递结束哨兵
    for _ in range(workers):
        q.put(None)
    for t in consumers:
        t.join()

    t_keys_done = time.time()
    # 关键路径已结束，此时第一个 key 早已过传播期 → 校验几乎瞬时
    verify_keys(results, verbose=verbose, log=make_log(0))

    # ── 槽位池结算 ──────────────────────────────────────────────
    if pool is not None and verbose:
        print(f"🔀 槽位池结算：{pool.describe()}", flush=True)
        bans = pool.stats()["bans"]
        if bans and len(bans) >= pool.size:
            # 每个出口都被封过至少一次 —— 这是**换订阅/换节点**的信号，
            # 不是"再加并发"的信号。加并发只会更快撞穿剩下的出口。
            print(f"   ⚠ {pool.size} 个槽位**每个**都被封过至少一次"
                  f"（累计 {sum(bans.values())} 次）—— 出口池整体在目标站点"
                  f"那边都不干净了。该换订阅或换节点，加并发无效。", flush=True)
        elif bans:
            clean = pool.size - len(bans)
            print(f"   仍有 {clean}/{pool.size} 个出口从未被封 —— "
                  f"把并发提到 {clean} 以内是有意义的。", flush=True)

    elapsed = round(time.time() - t_start, 1)
    for r in results:
        if r is not None:
            r.timings.setdefault("batch_total", round(elapsed * 1000))
            r.timings["keys_done"] = round((t_keys_done - t_start) * 1000)
    return [r for r in results if r is not None]
