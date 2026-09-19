"""数据形状：对外结果类型 + 一次尝试的内部状态容器。

- `LoginResult` 是**公开契约** —— `pipeline` / 探针 / 台账都读它的字段，
  字段名与顺序被 `tests/test_browser_login.py` 冻结。
- `_AttemptState` 是阶段 A 的产物：把原本散在 `_run_attempt` 里的 5 个共享 dict
  （jwt / code / captcha 统计 / 鼠标统计 / timings）+ 3 个闭包合并成一个对象，
  使 8 个步骤函数可以只传一个 `st` 参数。

⚠ 别把 `LoginResult` 当内部类型改字段名 —— 它是跨模块契约。
"""

import time
from dataclasses import dataclass, field


@dataclass
class LoginResult:
    ok: bool
    jwt: str = ""
    code: str = ""
    reason: str = ""
    cookies: dict = field(default_factory=dict)
    captcha_stage: dict = field(default_factory=dict)
    attempts_used: int = 0
    timings: dict = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────
# 一次登录尝试的状态与埋点
# ────────────────────────────────────────────────────────────────
class _AttemptState:
    """一次 `_run_attempt` 的全部可变状态 + 埋点。

    为什么要有这个类，而不是散着的几个 dict：

    `on_response` 是 Playwright 的**事件回调** —— 它由事件循环在任意时刻触发，
    与主流程之间只能通过共享容器通信。而主流程的多个循环又要在各自的
    `break` 点读同一个 `jwt`。这些容器一旦要跨函数传递，每个步骤函数的
    签名就会膨胀到 7~8 个参数。容器化之后，回调与每个步骤都只吃一个 `state`。

    ⚠ `cap` / `mv` 的**键名与形状必须逐字保持** —— 它们最终被展开进
      `LoginResult.captcha_stage`，探针与台账都依赖那些键。
    """

    def __init__(self, *, verbose: bool = False):
        self.verbose = verbose
        self.t_start = time.time()
        self.jwt = ""          # 原 `jwt_holder["jwt"]`
        self.code = ""         # 原 `code_holder["code"]`
        self.cap = {"init": 0, "verify": [], "last_ok": False, "payload": "",
                    "slider": "", "trivial": 0, "events": []}
        self.mv = {}           # 鼠标动作统计；原样传给 _warmup_mouse / _micro_move
        self.timings = {}      # 原 `tm`

    def mark(self, name: str) -> None:
        self.timings[name] = round((time.time() - self.t_start) * 1000)

    def ev(self, kind: str, detail: str = "") -> None:
        """细粒度事件时间线。

        只有把 InitCaptchaV3 / VerifyCaptchaV3 的**到达时刻**逐个记下来，
        才能回答"captcha_ready 这 8 秒到底花在哪一段" —— 单个粗粒度标记
        做不到归因，只能看到总时长变了却不知道谁变了。
        """
        self.cap["events"].append(
            [round((time.time() - self.t_start) * 1000), kind, detail])

    def on_response(self, resp) -> None:
        """页面响应回调：捞 JWT / 授权 code，并统计验证码交互。"""
        cap = self.cap
        try:
            hdrs = dict(resp.headers)
        except Exception:
            hdrs = {}
        auth = hdrs.get("authorization", "")
        if auth.startswith("Bearer ") and not self.jwt:
            self.jwt = auth[len("Bearer "):]
        u = resp.url
        if "internal/auth" in u and not self.code:
            try:
                self.code = (resp.json().get("data") or {}).get("code", "")
            except Exception:
                pass
        try:
            pd = resp.request.post_data or ""
        except Exception:
            pd = ""
        if "InitCaptchaV3" in pd:
            cap["init"] += 1
            self.ev(f"Init#{cap['init']}")
            if self.verbose:
                print(f"    [login] InitCaptchaV3 #{cap['init']}", flush=True)
        elif "VerifyCaptchaV3" in pd:
            try:
                res = (resp.json().get("Result") or {})
                vc = res.get("VerifyCode", "")
                ok = bool(res.get("VerifyResult"))
                cap["verify"].append(vc)
                cap["last_ok"] = ok
                # `trivial` = TRACELESS 预检**被拒**的次数。
                # ⚠ 判据必须同时满足"在 Init#1 阶段"和"ok=False"：
                #   TRACELESS 也可能**直接通过**（T001），那是最理想的结果
                #   （无需点击），把它算成 reject 会误读成"预检失败"。
                if cap["init"] <= 1 and not ok:
                    cap["trivial"] += 1
                if not ok:
                    cap["payload"] = pd[:500]
                self.ev(f"Verify#{len(cap['verify'])}", f"{vc} ok={ok}")
                if self.verbose:
                    print(f"    [login] VerifyCaptchaV3 -> {vc} ok={ok}", flush=True)
            except Exception:
                pass
