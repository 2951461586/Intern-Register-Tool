"""全局配置。

所有密钥类信息集中在此，可通过环境变量或项目根的 `.env` 文件覆盖。

🔴 凭据**不写死在代码里** —— 本项目托管在公开仓库，写死等于直接泄漏。
   首次使用：`cp .env.example .env`，填入真实值。`.env` 已在 .gitignore 中。
"""

import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """极简 `.env` 解析（stdlib 实现，不引 python-dotenv 依赖）。

    只填充「尚未存在于 os.environ」的键 —— 真实环境变量优先级更高，
    这样临时覆盖（`IR_XXX=1 python run.py`）依然生效。
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# ── CF Worker 临时邮箱 ────────────────────────────────────────────
WORKER_BASE = os.getenv("IR_WORKER_BASE", "")
# ⚠ 默认留空。缺它时由 `validate()` 在入口处报错，而不是静默发一堆 401。
WORKER_ADMIN_TOKEN = os.getenv("IR_WORKER_ADMIN_TOKEN", "")
WORKER_DOMAIN = os.getenv("IR_WORKER_DOMAIN", "")

# ── OpenXLab SSO ─────────────────────────────────────────────────
SSO_BASE = "https://sso.openxlab.org.cn"
SSO_GW = f"{SSO_BASE}/gw/uaa-be/api/v1"

# 注册时使用的应用身份（来自 discovery 活动页）
CLIENT_ID = "dagw07mkg1bazlxzoy31"
SOURCE = "discovery"

# RSA 公钥（SPKI/DER base64，服务端静态）
SSO_PUBKEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCOst3X5k3uqRpKtFOfLQdh5ZyakdP0fnP6CyPs"
    "9e2BWF/Jud+BZNNWOPtm5roUu3Cf0wFbvha4uD+XxmNz/3Ea+VOrfbhIeSWX3CTZ+9oAWERz0ftF"
    "oEYTf2nAt5LORhhNHt2Wea8yMTD8GoZ/asm2GX3B/CjIa6PwVlbRHX9/bwIDAQAB"
)

# ── Discovery 平台（API Key 与额度）────────────────────────────────
DISCOVERY_BASE = "https://discovery.intern-ai.org.cn"
DISCOVERY_API = f"{DISCOVERY_BASE}/api"

# ── API Key 专用推理网关 ──────────────────────────────────────────
# 🔴 别用 chat.intern-ai.org.cn —— 那是网页版聊天后端，只认 SSO JWT，
#    且要求账号绑定手机号（-20035），拿 sk- key 打会得到 401 A0211。
#    真正接受 sk- key 的是下面这个主机，路径前缀 /v1（OpenAI 兼容）。
CHAT_API_BASE = os.getenv(
    "IR_CHAT_API_BASE", "https://discovery-api.intern-ai.org.cn/v1"
)

# TokenPlan 可用模型（2026-09-15 实测，来自 GET /v1/models）
# ⚠ intern-s1 不在其中，用它会得到 model_not_available
CHAT_MODELS = [
    "deepseek-v4-flash-0731",
    "minimax-m3",
    "deepseek-v4-flash-vision",
    "qwen3.8-27b",
    "intern-s2",
    "deepseek-v4-pro-0813",
    "Agents-A1",
    "Atria-Dawn-Preview",
    "glm-5.3",
    "kimi-k2.6",
]

# 活动邀请信息（注册跳转链接中的参数）
INVITER_USER_ID = "415100755"
INVITER_USERNAME = "OpenXLab-ymAqeOKDn"
ACTIVITY_PATH = "activity/reasearch-acceleration-camp"

# ── 行为参数 ──────────────────────────────────────────────────────
# 邮件实测在注册后 3 秒内到达；轮询间隔 0.8s 可在 1~2 次内命中，
# 而收信接口单次往返 ~600ms，再密就只是在打 Cloudflare。
MAIL_POLL_INTERVAL = 0.8    # 秒
MAIL_POLL_TIMEOUT = 120     # 秒
#
# 🔴 2026-09-19 改造：收信改走 `/api/inbox?email=`，**不再走 `/admin/all`**。
#
#   为什么必须换（两条，第二条才是要命的）：
#     1. 读配额：实测 `rows_read` —— `/admin/all?limit=50` 读 **51 行**，
#        而 `/api/inbox?email=`（命中 1 封）只读 **2 行**、没命中读 **0 行**。
#        09-18 D1 读配额被烧到 173.9%（8,695,305 / 5,000,000）就是这个接口干的。
#     2. **窗口截断**：`/admin/all` 返回的是「最新 N 条」，而 D1 的 retention
#        只保留 100 行。实测这张表被**同机另一个项目**（grok 注册线，共用同一个
#        Worker）以 **21.6 封/小时**灌满 ⇒ 整表每 4.6 小时被冲刷一遍。
#        我们的激活邮件一旦被挤出「最新 N 条」窗口就**永远读不到**。
#        `/api/inbox` 按收件人索引查，别人的邮件挤不掉我们的。
#
#   下面这两个常量现在**只服务于向后兼容**（`list_mails()` 不传 email 时的
#   退回路径）。收信主路径已经不需要"窗口开多大"这个折中了 ——
#   索引查询只返回这一个收件人的邮件。
MAIL_LIST_MIN = 5           # 仅退回路径用（历史值，保留不动）
MAIL_LIST_LIMIT = 50        # 仅退回路径用（`list_mails()` 不传 email 时的默认 limit）
REQUEST_TIMEOUT = 30        # 秒

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

# 本机 Chrome（Playwright 驱动，避免下载额外浏览器）
CHROME_PATH = os.getenv(
    "IR_CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)


# ── 注册配额保护（本地累计计数）───────────────────────────────────
# 🔴 `register/byEmail` 有**两层**限制，别只看到速率那层：
#     ① 瞬时速率：4 路并发同时到达 → 3 路立刻 429
#        → 由 `pipeline.REG_MIN_INTERVAL` 闸门控制
#     ② 累计配额：同一时段累计约 40 个后开始 `B0000 请求频繁`，
#        之后连**单账号**都注册不了
#        → 闸门**完全无效**，只能主动停下（见 `src/quota.py`）
#
# ⚠ 窗口长度的实测依据（2026-09-15 两次观测，**推翻了最初的"数分钟"假设**）：
#     最后成功注册 14:05:43 → 22:39（**8.6 小时后**）单账号探测**仍然 B0000**。
#     所以恢复窗口 **> 8.6h**，最初取的 6h **不是保守、而是太短** ——
#     它会让保护在服务端仍封着时放行。
#     现在取 24h 作为**保守上界**（很可能是"每天 N 个"这类日历窗口，
#     真实恢复点待测；滚动 24h 在恢复时间未知时只会多拦、不会漏拦）。
#
# 下面两个值决定 ② 的保护策略。默认上限来自实测（触顶点约 40 个）。
# ⚠ 这是本地保护，不是权威计量：换机器 / 删 state 文件都会重置。
#   被误拦时用 `run.py --ignore-quota` 或调大 `IR_REG_QUOTA_MAX` 放行。
REG_QUOTA_MAX = int(os.getenv("IR_REG_QUOTA_MAX", "40"))
REG_QUOTA_WINDOW_H = float(os.getenv("IR_REG_QUOTA_WINDOW_H", "24"))


# ── 出口代理（换 IP 绕过 IP 维度的封禁）───────────────────────────
# 注册封禁是 **IP 维度**（见 README「封禁是 IP 维度」），所以换出口 IP 是对症的解法。
#
# 🔴 实测背景（2026-09-16）：本机默认出口是 **机房 IP**（203.0.113.20，
#    NTT America / 天风通信，洛杉矶）—— 这类 IP 信誉差，很可能就是被封的原因。
#    住宅 IP（Google Fiber / Spectrum 等家宽）信誉高得多。
#
# 格式（与常见代理商控制台一致）：
#     IR_PROXY=host:port:user:pass          # 单条
#     IR_PROXY=http://user:pass@host:port   # 也接受完整 URL
#
# ⚠ 三条硬规矩（都是实测踩出来的，见 `tools/probe_proxy.py`）：
#   1. **别用 TCP 连通性判断代理是否可用**。本机跑着 Clash TUN，连接
#      `203.0.113.30:764` 其实是连到本地虚拟网卡（实测 0.02s —— 中国到
#      美国不可能是 20ms）。必须真的发一个请求拿到出口 IP 才算数。
#   2. **别只看状态码**。代理的域名 ACL 拒绝时返回 `403` +
#      `errorMsg: <host>:80 not accessible`，正文才是判据。
#   3. **必须拿真实目标域名试**。一个代理能通 google/baidu，不代表能通
#      `sso.openxlab.org.cn` —— 实测有代理商精确屏蔽了 `openxlab.org.cn`
#      和 `intern-ai.org.cn`。
IR_PROXY = os.getenv("IR_PROXY", "").strip()

# ── 槽位代理池（一槽一端口，用于绕 IP 维度封禁）────────────────────
# 🔴 这是本项目的**规模化杠杆**：注册封禁是 IP 维度累计配额，
#    而旧实现只有一条 `IR_PROXY` —— 也就是整个批次共用**一个**出口 IP，
#    `workers` 调到几都一样会撞配额。真正的约束是"有多少个不同出口 IP"。
#
# 来源优先级：`IR_PROXY_SLOTS_FILE` > `IR_PROXY_SLOTS`。
# 槽位多时（20~50 个）用文件，别把 50 个 URL 塞进环境变量。
IR_PROXY_SLOTS = os.getenv("IR_PROXY_SLOTS", "").strip()
IR_PROXY_SLOTS_FILE = os.getenv("IR_PROXY_SLOTS_FILE", "").strip()
# 一个槽位被判"这个 IP 被目标站点封了"后，冷却多久才重新启用（秒）。
# 参考 aBaiFreeGPT 的 `MIHOMO_NODE_COOLDOWN_SECONDS=120`。
IR_PROXY_COOLDOWN = float(os.getenv("IR_PROXY_COOLDOWN", "120") or 120)
# 等一个空闲槽位最多等多久（秒）。全部槽位都在冷却时 `acquire()` 会阻塞，
# 这个值就是它的上限 —— 超时抛 `TimeoutError`，那个任务按失败记账。
# 🔴 别设成 0/无穷：0 会让"全冷却"瞬间变成一片假失败；
#    无穷会让批量任务永远挂在那里，看不出是卡死了。
IR_PROXY_SLOT_TIMEOUT = float(os.getenv("IR_PROXY_SLOT_TIMEOUT", "240") or 240)
# 起飞前是否检查槽位监听端口是否活着（`1` 开 / `0` 关，默认开）。
# 🔴 为什么默认开：槽位实例是**独立的前台进程**，很容易"配置还留着、进程已经没了"。
#    那种状态下每条注册记录都会以**代理连接错误**收场，而在这个项目里
#    "注册全失败"最容易被误读成"换 IP 也不行 / 还在封" —— 结论完全错。
#    6 个本地端口的 TCP 连通检查是毫秒级的，代价可以忽略。
IR_PROXY_PREFLIGHT = os.getenv("IR_PROXY_PREFLIGHT", "1").strip().lower() \
    not in ("0", "false", "no", "off")


def proxy_slots() -> list[str]:
    """读槽位清单。**没配置就返回空列表**（调用方据此退回单代理行为）。

    文件里允许一行一个，也允许逗号分隔；`#` 开头视为注释。
    """
    raw = ""
    if IR_PROXY_SLOTS_FILE:
        p = Path(IR_PROXY_SLOTS_FILE)
        if not p.is_file():
            raise ValueError(f"IR_PROXY_SLOTS_FILE 指向的文件不存在：{p}")
        raw = p.read_text(encoding="utf-8")
    elif IR_PROXY_SLOTS:
        raw = IR_PROXY_SLOTS
    out: list[str] = []
    for line in raw.replace("\n", ",").split(","):
        item = line.strip()
        if item and not item.startswith("#"):
            out.append(item)
    return out


# ── 槽位端口 -> 出口 IP（配额记账的维度）────────────────────────────
# 🔴 配额必须按**出口 IP** 记，绝不能按槽位号（`slot1`/`slot2`）记。
#
#    为什么：目标站点是按 **IP** 封的，而 `slots.txt` 的条目顺序随时会变
#    （加出口、删死掉的出口、调顺序）。用"位置号"当 scope 时，
#    `slots.txt` 一改动，既有记录就**整体错配到别的 IP 头上** ——
#    而且不报错、不抛异常，只是让某个 IP 偷偷超限、另一个被提前停掉。
#
#    本项目实测已经踩到过：`slots.txt` 从 4 项变 6 项又变 3 项的过程中，
#    台账里出现了 3 条错配（某个 scope 混进了别的槽位的记录）。
#    用出口 IP 当 scope 后，池子顺序怎么排都不影响记账。
#
# 🔴 这张表**不进仓库**（出口 IP 属于基础设施标识，公开可被搜索，
#    节点容易被盯上）。放在 `.env` 里：
#
#        IR_SLOT_EGRESS_IPS=7901=10.0.0.1,7902=10.0.0.2
#
# 🔴 换订阅 / 换节点 / 换机房之后这张表就过期了，必须重测：
#        python tools/probe_slots.py
#    改完还要**同步迁移台账**（否则旧记录挂在旧 IP 名下）：
#        python tools/migrate_quota_scope.py --apply
IR_SLOT_EGRESS_IPS = os.getenv("IR_SLOT_EGRESS_IPS", "").strip()


def _parse_slot_egress(raw: str) -> dict:
    """把 `7901=1.2.3.4,7902=5.6.7.8` 解析成 `{端口: IP}`。

    容忍换行分隔与空项；格式不对的项直接跳过（不抛），
    因为缺项会在 `slot_scope()` 里被更明确地报出来。
    """
    out: dict = {}
    for item in raw.replace("\n", ",").split(","):
        item = item.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        port, _, ip = item.partition("=")
        port, ip = port.strip(), ip.strip()
        if port and ip:
            out[port] = ip
    return out


SLOT_EGRESS_IPS: dict = _parse_slot_egress(IR_SLOT_EGRESS_IPS)


def slot_scope(url: str) -> str:
    """把槽位 URL 映射成**配额记账的 scope**（= 那个槽位的出口 IP）。

    出口 IP 才是目标站点真正封的那个东西，也是唯一不随配置漂移的标识。
    用位置号（`slot1`/`slot2`）当 scope 是错的 —— 见 `SLOT_EGRESS_IPS` 的说明。

    🔴 端口不在映射表里时**大声报错，不退回位置号**：
    静默错配会让某个 IP 悄悄超过上限（真被目标站封），
    比"跑不起来、逼你去补映射"危险得多。
    """
    port = url.rsplit(":", 1)[-1].strip()
    ip = SLOT_EGRESS_IPS.get(port)
    if not ip:
        raise ValueError(
            f"槽位 {url} 的出口 IP 未知（端口 {port} 不在 SLOT_EGRESS_IPS 里）。\n"
            f"  1) 先量出真实出口 IP： python tools/probe_slots.py\n"
            f"  2) 写进 .env（**不要写进源码**，出口 IP 不进仓库）：\n"
            f"       IR_SLOT_EGRESS_IPS={port}=<那个槽位的出口 IP>\n"
            f"     多个槽位用逗号分隔：7901=1.1.1.1,7902=2.2.2.2\n"
            f"  3) 迁移台账： python tools/migrate_quota_scope.py --apply\n"
            f"  —— 不能退回按槽位号记账：那会静默把配额记到别的 IP 头上。")
    return ip


def proxies(raw: str = None) -> dict | None:
    """把代理串解析成 requests 的 `proxies` 字典；空则返回 `None`。

    支持两种写法：
        host:port:user:pass          → http://user:pass@host:port
        scheme://user:pass@host:port → 原样使用（可指定 socks5）

    `raw` 传 None 时用全局 `IR_PROXY`。**传具体值时不碰全局状态** ——
    槽位池在并发场景下必须这样用，否则多线程改 `IR_PROXY` 会互相踩。
    """
    raw = (IR_PROXY if raw is None else raw).strip()
    if not raw:
        return None
    if "://" not in raw:
        parts = raw.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            raw = f"http://{user}:{pwd}@{host}:{port}"
        elif len(parts) == 2:
            raw = f"http://{raw}"
        else:
            raise ValueError(
                f"代理串格式无法识别：{raw!r}（期望 host:port:user:pass "
                f"或 scheme://user:pass@host:port）")
    return {"http": raw, "https": raw}


def apply_proxy(session, proxy: str = None) -> None:
    """把代理挂到 `requests.Session` 上（未配置则什么都不做）。

    `proxy` 传 None 时用全局 `IR_PROXY`；传具体值则只作用于这个 session
    （槽位池并发场景必须走这条，见 `proxies()` 的说明）。

    🔴 **必须同时关掉 `trust_env`** —— 否则环境变量会**静默盖掉**
    `session.proxies`。这是实测踩出来的（2026-09-16），机制在
    `requests/sessions.py` 的 `merge_environment_settings`：

        env_proxies = get_environ_proxies(url)      # 先读环境变量
        for k, v in env_proxies.items():
            proxies.setdefault(k, v)                # ← 环境变量先进字典
        proxies = merge_setting(proxies, self.proxies)   # ← 已存在的键不覆盖

    所以 `session.proxies` 的优先级**低于**环境变量。本机有
    `HTTP_PROXY=http://127.0.0.1:7897`（Clash），于是实测：

        session.proxies = {...}                    → 被忽略，走了 Clash  ✗
        session.proxies = {...} + trust_env=False  → 生效               ✓
        session.get(url, proxies={...})            → 生效（per-request 优先级最高）

    现象特别隐蔽：请求**成功了**（200），只是**没走你指定的代理** ——
    既不报错也不告警，很容易误判成"代理已生效"。

    ⚠ 未配置代理时**不动 `trust_env`**：那样环境代理（Clash）仍是
      默认出口，与加这个功能之前的行为一致。
    """
    px = proxies(proxy)
    if not px:
        return
    session.proxies = px
    session.trust_env = False


# ── 启动校验 ──────────────────────────────────────────────────────
def validate(*, need_worker_token: bool = True) -> list[str]:
    """返回缺失的必需配置项（空列表 = 就绪）。

    刻意**不在 import 时抛错** —— 那样连 `--help` 和离线分析都跑不起来。
    由入口显式调用，报错时直接给出修法。
    """
    missing = []
    if need_worker_token and not WORKER_ADMIN_TOKEN:
        missing.append("IR_WORKER_ADMIN_TOKEN")
    # 这两项不再有写死的默认值（本仓库是公开的），缺失时在入口报错，
    # 而不是带着空 base 去发一堆注定失败的请求。
    if not WORKER_BASE:
        missing.append("IR_WORKER_BASE")
    if not WORKER_DOMAIN:
        missing.append("IR_WORKER_DOMAIN")
    return missing

