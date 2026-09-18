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

from . import config
from .discovery import DiscoveryClient
from .sso import SSOClient
from .tempmail import TempMailClient


def create_mail_client():
    """按配置返回临时邮箱客户端（worker=CF Worker，yyds=YYDS Mail）。

    两者接口兼容：create_mailbox(domain, count) -> list[str]，
    wait_for_mail(address) -> 带 find_link / received_at 的邮件对象。
    """
    if config.MAIL_PROVIDER == "yyds":
        from .yyds_client import YydsMailClient

        return YydsMailClient(
            api_key=config.YYDS_API_KEY,
            base_url=config.YYDS_BASE_URL,
            domain=config.YYDS_DOMAIN,
            subdomain=config.YYDS_SUBDOMAIN,
        )
    return TempMailClient()

# 注册阶段的并发度。纯 HTTP，可以给得比浏览器侧高。
# 注意：并发度 ≠ 注册速率 —— 速率由下面的 REG_MIN_INTERVAL 闸门控制。
REG_CONCURRENCY = 4

# 两次 `register/byEmail` 之间的最小间隔（秒）。
# 🔴 实测 `register/byEmail` 有写操作限流：4 路**并发**（同时到达）时 3 路立刻被
#    429 拒绝，而只读的 `personal/username/check` 8 路并发也不限流。
#    → 限流挂在**写操作 + 突发**上，不是笼统的 IP 速率限制。
#
# 边界实测（`.workbuddy-ai/tmp/probe_reg_interval.py`，绕开退避重试打裸请求，
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
    stages: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    created_at: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


# ────────────────────────────────────────────────────────────────
# Stage 1+2：建邮箱 → 注册 → 收信激活（纯 HTTP）
# ────────────────────────────────────────────────────────────────
def stage_register(mail: TempMailClient, sso: SSOClient, rec: AccountRecord,
                   *, mail_domain: str = None, log=print, gate=None) -> bool:
    """gate: 可选的限速闸门（callable）。在真正调用 register/byEmail 前触发，
    用于把注册速率钉死在安全区间内（见 REG_MIN_INTERVAL）。

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

        t = time.time()
        reg = sso.register(rec.username, rec.email, rec.password)
        mark("register_call", t)
        if not reg.ok:
            raise RuntimeError(f"register failed: {reg.msg_code} {reg.msg}")
        rec.sso_uid = reg.sso_uid
        rec.stages["register"] = "ok"
        log(f"registered: uid={rec.sso_uid} user={rec.username}")
    except Exception as ex:
        rec.status = "failed"
        rec.error = f"register: {ex}"
        rec.timings["register"] = round((time.time() - t0) * 1000)
        rec.timings["register_detail"] = sub
        return False

    t_reg_done = time.time()
    try:
        t = time.time()
        m = mail.wait_for_mail(rec.email)
        mark("mail_wait", t)
        if not m:
            raise RuntimeError("activation mail not received within timeout")

        # 🔬 把 `mail_wait` 拆成"邮件真正到达"与"轮询开销"两段。
        # 不拆就是在猜 —— 7.87s 到底是 SMTP/Worker 慢，还是我们的轮询太钝，
        # 两者的修法完全不同（前者无解，后者调 limit/interval 即可）。
        # `received_at` 是**毫秒** unix 时间戳（实测 1789449135216）。
        arrival = m.received_at / 1000.0 if m.received_at > 1e11 else float(m.received_at)
        sub["arrival_delay_ms"] = round((arrival - t_reg_done) * 1000)
        sub["poll_overhead_ms"] = sub["mail_wait"] - sub["arrival_delay_ms"]

        link = m.find_link("active", "activat", "verif", "confirm")
        if not link:
            raise RuntimeError("activation link not found in mail")

        t = time.time()
        if not sso.activate_from_url(link):
            raise RuntimeError("activate returned success=false")
        mark("activate_call", t)
        rec.stages["activate"] = "ok"
        log("activated")
    except Exception as ex:
        rec.status = "failed"
        rec.error = f"activate: {ex}"
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
            from .browser_login import login as browser_login

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
    mail, sso = create_mail_client(), SSOClient()
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
              reg_concurrency: int = None) -> list[AccountRecord]:
    """批量注册，注册阶段与浏览器阶段流水线并行。

    Args:
        count: 账号数量
        workers: 浏览器并发数。**这是受风控约束的参数**，不要盲目调大。
                 workers=1 等价于顺序执行（最保守）。
        headless: 无头模式
        reg_concurrency: 注册阶段**线程并发度**，默认 4。注意这不等于注册
                 **速率** —— 速率由 REG_MIN_INTERVAL 闸门控制（写操作限流）。
                 并发度给高是为了让建邮箱、收信轮询并行。

    Returns:
        与提交顺序一致的 AccountRecord 列表
    """
    if workers < 1:
        raise ValueError("workers must be >= 1")
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
        from .browser_login import BrowserSession

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
                        if rec.status == "failed":
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
                finally:
                    q.task_done()

    consumers = [threading.Thread(target=consumer_worker, args=(i,), daemon=True)
                 for i in range(workers)]
    for t in consumers:
        t.start()

    # ── 生产者：并发注册 + 激活，完成一个就投递一个 ────────────
    # 注册速率由闸门钉死（register/byEmail 有写操作限流，见 REG_MIN_INTERVAL）
    reg_gate = _RateLimiter(REG_MIN_INTERVAL)

    def producer(idx: int):
        rec = AccountRecord(created_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        results[idx] = rec
        log = make_log(idx)
        try:
            mail, sso = create_mail_client(), SSOClient()
            stage_register(mail, sso, rec, mail_domain=mail_domain, log=log,
                           gate=reg_gate)
        except Exception as ex:
            rec.status = "failed"
            rec.error = f"register: {ex}"
        finally:
            q.put((idx, rec))

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=reg_conc) as ex:
        list(ex.map(producer, range(count)))

    # 投递结束哨兵
    for _ in range(workers):
        q.put(None)
    for t in consumers:
        t.join()

    t_keys_done = time.time()
    # 关键路径已结束，此时第一个 key 早已过传播期 → 校验几乎瞬时
    verify_keys(results, verbose=verbose, log=make_log(0))

    elapsed = round(time.time() - t_start, 1)
    for r in results:
        if r is not None:
            r.timings.setdefault("batch_total", round(elapsed * 1000))
            r.timings["keys_done"] = round((t_keys_done - t_start) * 1000)
    return [r for r in results if r is not None]
