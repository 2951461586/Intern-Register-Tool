"""一次登录尝试：8 个步骤 + 结果构造 + 编排。

切点不是新发明的 —— 来自原 `_run_attempt` 内部注释的编号 `1)~8)`
与 `mark()` 埋点（见 docs/refactor-14-browser-split-plan.md §1.4）。

分层：本模块依赖 constants / state / behavior / captcha / urls，
**不被它们反向依赖**（`session` 才用 `_run_attempt`）。
"""

import random
import time

from .behavior import _idle_wait, _micro_move, _warmup_mouse
from .captcha import _click_checkbox, _has_slider
from .constants import (
    ANTI_DETECT_JS,
    MAX_CLICKS_PER_ATTEMPT,
    MICRO_BUDGET_S,
    MICRO_MOVE,
    PREWARM_MS,
    TYPE_DELAY_HI,
    TYPE_DELAY_LO,
    UI_WAIT_MS,
)
from .state import LoginResult, _AttemptState
from .urls import build_login_url


# ────────────────────────────────────────────────────────────────
# 一次尝试的 8 个步骤
# 切点不是我发明的 —— 来自原注释里的编号 `1)~8)` 与 mark() 埋点
# ────────────────────────────────────────────────────────────────
def _step_open_form(page, st: _AttemptState):
    """导航到登录页 → 切"账号登录" → 切"密码登录" tab，直到输入框就绪。

    返回 `acc_box`（账号输入框 locator），供 `_step_type_credentials` 复用 ——
    原实现就是在这一步创建、下一步复用的，这里保持同一对象。
    """
    page.goto(build_login_url(), wait_until="domcontentloaded", timeout=60000)
    st.mark("goto")

    # 🔬 预加载实验：页面加载后先闲置一段再操作（原理见 PREWARM_MS）。
    #    默认 0，不进这个分支 —— 生产行为与实验前完全一致。
    if PREWARM_MS > 0:
        page.wait_for_timeout(PREWARM_MS)
        st.mark("prewarm")

    # 1) 切到账号登录。
    #    自适应等待元素可点，替代原先固定的 2.5~4.2s —— 快且更稳
    #    （慢机器上固定 sleep 反而不够）。
    entry = page.get_by_text("使用手机号 / 密码登录", exact=False).first
    entry.wait_for(state="visible", timeout=UI_WAIT_MS)
    entry.click(timeout=8000)

    # 2) 切到密码登录 tab，等输入框真正就绪
    tab = page.get_by_text("密码登录", exact=True).first
    tab.wait_for(state="visible", timeout=UI_WAIT_MS)
    tab.click(timeout=8000)
    acc_box = page.locator("#normal_login_account")
    acc_box.wait_for(state="visible", timeout=UI_WAIT_MS)
    # 保留一点自然停顿（真人不会 0ms 内连续操作），但远短于原先的固定值
    page.wait_for_timeout(random.randint(120, 300))
    st.mark("form_ready")
    return acc_box


def _step_type_credentials(page, st: _AttemptState, acc_box, account: str,
                           password: str) -> None:
    """逐字输入账号与密码。"""
    # 3) 逐字输入（用 type 而非 fill —— fill 不产生任何键盘事件）。
    #    ⚠ 按键间隔见 `TYPE_DELAY_LO/HI` 的说明：默认值来自人类打字的量级，
    #      但"能不能调小"必须实测，不要当成铁律。
    acc_box.click(timeout=10000)
    page.keyboard.type(account, delay=random.randint(TYPE_DELAY_LO, TYPE_DELAY_HI))
    page.wait_for_timeout(random.randint(180, 420))
    pwd_box = page.locator("#normal_login_password")
    pwd_box.click(timeout=10000)
    page.keyboard.type(password, delay=random.randint(TYPE_DELAY_LO, TYPE_DELAY_HI))
    page.wait_for_timeout(random.randint(220, 520))
    st.mark("typed")


def _step_accept_agreements(page, st: _AttemptState) -> None:
    """勾选全部协议类复选框。"""
    # 4) 勾选全部协议类复选框
    for i in range(page.locator("input[type=checkbox]").count()):
        el = page.locator("input[type=checkbox]").nth(i)
        try:
            if not el.is_checked():
                el.check(timeout=5000)
        except Exception:
            pass
    st.mark("checkbox")


def _step_warmup(page, st: _AttemptState, vw: int, vh: int) -> tuple:
    """提交前的短暖场（2~3 轮），返回新光标位置。"""
    # 5) 短暖场（2~3 轮）。长时间鼠标活动改在下面等验证码时做
    cur = _warmup_mouse(page, vw, vh, stats=st.mv)
    st.mark("warmup")
    return cur


def _step_submit(page, st: _AttemptState) -> None:
    """点登录按钮。"""
    # 6) 提交
    page.locator("button").filter(has_text="登录").first.click(timeout=10000)
    st.ev("submit")


def _step_wait_captcha(page, st: _AttemptState, *, timeout: int, vw: int, vh: int,
                       cur: tuple, verbose: bool) -> tuple:
    """等验证码 SDK 出结果，返回 `(新光标位置, 等待毫秒数)`。"""
    cap = st.cap
    # 7) 等验证码出结果。
    #    两条可能的通路（哪个先到算哪个）：
    #      Path A  TRACELESS 预检**自己通过**（Verify#1 = T001）→ 0 次点击
    #      Path B  TRACELESS 被拒（F001）→ Init#2 降级 CHECK_BOX → 走点击
    #    ⚠ 循环条件用**真实时间**而非计数器：`_micro_move` 单次耗时
    #      随轨迹长度浮动（~150~350ms），用 `waited += 250` 计数的话
    #      实验组会比真实时间慢 8% 左右，两组就不可比了。
    #    🔴 `MICRO_BUDGET_S` 之后转入安静期（`_idle_wait`）：见常量处的
    #      对照实验数据 —— 一直喂数据会让 TRACELESS 迟迟不认输，
    #      安静下来才能逼它下降级决定，把最坏情况钉住。
    t_cap = time.time()
    while (time.time() - t_cap) < timeout and cap["init"] < 2:
        if st.jwt:
            break
        if (time.time() - t_cap) < MICRO_BUDGET_S:
            cur = _micro_move(page, cur, vw, vh, stats=st.mv)
        else:
            if not st.mv.get("budget_logged"):
                st.mv["budget_logged"] = True
                st.ev("micro_budget_exhausted", f"{MICRO_BUDGET_S}s")
                if verbose:
                    print(f"    [login] 微移动预算用尽（{MICRO_BUDGET_S}s），"
                          f"转入安静期等 SDK 降级", flush=True)
            _idle_wait(page, stats=st.mv)
    waited = round((time.time() - t_cap) * 1000)
    st.mark("captcha_ready")
    st.ev("captcha_ready", f"waited={waited}ms init={cap['init']}")
    if verbose:
        print(f"    [login] init count = {cap['init']} "
              f"(waited {waited / 1000:.1f}s, traceless_reject={cap['trivial']}, "
              f"micro_moves={st.mv.get('moves', 0)})", flush=True)
    return cur, waited


def _step_click_until_jwt(page, st: _AttemptState, *, vw: int, vh: int, cur: tuple,
                          verbose: bool) -> tuple:
    """点复选框（最多 `MAX_CLICKS_PER_ATTEMPT` 轮），返回新光标位置。"""
    cap = st.cap
    # 8) 点击复选框，最多 MAX_CLICKS_PER_ATTEMPT 轮
    for attempt in range(MAX_CLICKS_PER_ATTEMPT):
        if st.jwt:
            break
        cur, clicked = _click_checkbox(page, cur)
        st.ev(f"click#{attempt + 1}", f"clicked={clicked}")
        if verbose:
            print(f"    [login] click #{attempt + 1} at "
                  f"({cur[0]:.0f},{cur[1]:.0f}) clicked={clicked}", flush=True)
        if not clicked:
            page.wait_for_timeout(1200)
            continue

        # 等本轮结果（必须 pump 事件）。同时继续做小幅移动，
        # 让"正在等待"这件事本身也表现为真人行为。
        for _ in range(10):
            page.wait_for_timeout(1000)
            if st.jwt:
                break
            cur = _micro_move(page, cur, vw, vh, stats=st.mv)
        if st.jwt:
            break
        sl = _has_slider(page)
        if sl:
            cap["slider"] = sl
            st.ev("slider", sl)
            if verbose:
                print(f"    [login] ⚠ 弹出二次验证: {sl}", flush=True)
            break
        page.wait_for_timeout(random.randint(400, 900))
    st.mark("done")
    if st.jwt:
        st.ev("jwt")
    return cur


def _build_result(st: _AttemptState, *, cookies: dict, waited: int) -> LoginResult:
    """把 state 折成 `LoginResult`。

    ⚠ `captcha_stage` 的键名与形状是**下游契约**（探针读、台账存），勿改。
    """
    cap = st.cap
    jwt = st.jwt or cookies.get("uaa-token", "")
    reason = ""
    if not jwt:
        if cap["slider"]:
            reason = f"secondary captcha required ({cap['slider']})"
        elif cap["verify"]:
            reason = f"captcha rejected ({cap['verify'][-1]})"
        else:
            reason = "no jwt captured"
    return LoginResult(
        ok=bool(jwt), jwt=jwt, code=st.code, reason=reason,
        cookies=cookies,
        captcha_stage={"init": cap["init"], "verify": cap["verify"],
                       "last_ok": cap["last_ok"], "slider": cap["slider"],
                       "traceless_reject": cap["trivial"],
                       "payload": cap["payload"],
                       "events": cap["events"],
                       # Path A = TRACELESS 自过（0 点击）；Path B = 降级 CHECK_BOX
                       "path": ("A" if cap["init"] <= 1 and cap["last_ok"]
                                else ("B" if cap["init"] >= 2 else "?")),
                       "mouse": {"micro_move": MICRO_MOVE,
                                 "budget_s": MICRO_BUDGET_S,
                                 "moves": st.mv.get("moves", 0),
                                 "points": st.mv.get("points", 0),
                                 "idle_waits": st.mv.get("idle_waits", 0),
                                 "budget_exhausted": bool(
                                     st.mv.get("budget_logged"))},
                       "captcha_wait_ms": waited},
        timings=st.timings,
    )


# ────────────────────────────────────────────────────────────────
# 在给定 browser 上跑一次尝试（自带 context 生命周期）
# ────────────────────────────────────────────────────────────────
def _run_attempt(browser, *, account: str, password: str, headless: bool,
                 timeout: int, screenshot_prefix: str = None,
                 verbose: bool = False, tag: str = "") -> LoginResult:
    """在给定 browser 上跑一次登录尝试。

    这里**只负责编排**：建 context → 注册响应回调 → 依次调用 8 个步骤 → 折结果。
    每一步的实现见上面的 `_step_*`，状态在 `_AttemptState`。
    """
    st = _AttemptState(verbose=verbose)

    # 不覆盖 viewport：见文件头第 7 条
    ctx_kwargs = dict(
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        viewport=None,
        color_scheme="light",
    )
    if headless:
        # 唯一的 UA 覆盖：去掉 HeadlessChrome 自我标记，版本号原样保留。
        # Chrome 的 reduced UA 只用主版本号（Chrome/152.0.0.0），
        # 而 browser.version 是完整版本（152.0.7977.83），故取首段。
        major = (browser.version or "").split(".")[0]
        if major:
            ctx_kwargs["user_agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{major}.0.0.0 Safari/537.36"
            )
    ctx = browser.new_context(**ctx_kwargs)
    ctx.add_init_script(ANTI_DETECT_JS)
    page = ctx.new_page()
    page.on("response", st.on_response)

    try:
        acc_box = _step_open_form(page, st)
        _step_type_credentials(page, st, acc_box, account, password)
        _step_accept_agreements(page, st)

        vw, vh = page.evaluate("[window.innerWidth, window.innerHeight]")
        cur = _step_warmup(page, st, vw, vh)

        if screenshot_prefix:
            page.screenshot(path=f"{screenshot_prefix}{tag}_filled.png")

        _step_submit(page, st)
        cur, waited = _step_wait_captcha(page, st, timeout=timeout, vw=vw, vh=vh,
                                         cur=cur, verbose=verbose)
        _step_click_until_jwt(page, st, vw=vw, vh=vh, cur=cur, verbose=verbose)

        if screenshot_prefix:
            page.screenshot(path=f"{screenshot_prefix}{tag}_done.png")

        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        return _build_result(st, cookies=cookies, waited=waited)
    except Exception as ex:
        try:
            if screenshot_prefix:
                page.screenshot(path=f"{screenshot_prefix}{tag}_error.png")
        except Exception:
            pass
        return LoginResult(ok=False, reason=f"{type(ex).__name__}: {ex}"[:200],
                           captcha_stage={"init": st.cap["init"],
                                          "verify": st.cap["verify"],
                                          "events": st.cap["events"]},
                           timings=st.timings)
    finally:
        try:
            ctx.close()
        except Exception:
            pass
