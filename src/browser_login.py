"""浏览器登录模块 —— 通过本机 Chrome 完成 OpenXLab SSO 登录并提取 JWT。

为什么需要浏览器：
  `login/byAccount` 强制阿里云验证码 2.0（错误码 B0501「人机验证失败」），
  该验证码的 captchaVerifyParam 依赖设备指纹与阿里云签发的 securityToken，
  纯 HTTP 无法伪造（DeviceToken 末段是服务端签名）。实测所有登录入口
  （byAccount / byPhone / getSmsCode）都要人机验证，因此必须借助真实浏览器。

────────────────────────────────────────────────────────────────────
🔴 反检测策略：**最小化注入**（2026-09-15 实测修正）
────────────────────────────────────────────────────────────────────
本机真实 Chrome 为 152.0.7977.83。实测（见 .workbuddy-ai/tmp/probe_env.py）：
只加 `--disable-blink-features=AutomationControlled` 时，Chrome **原生**就是
    navigator.webdriver === false
    navigator.plugins    → 长度 5 的真 PluginArray
    window.chrome        → 存在
    hasCdc               → []（无 selenium 残留）

因此**绝对不要**再去"补"这些属性。早期版本注入的下面这些写法反而在制造破绽：
    navigator.plugins = [1,2,3,4,5]        # 类型从 PluginArray 变成普通 Array
                                           # plugins[0].name === undefined → 一眼假
    navigator.permissions.query = ...      # 返回普通对象，不是 PermissionStatus
    navigator.hardwareConcurrency = 8      # 真值 12 被改小
    navigator.deviceMemory = 8             # 原生是 undefined，凭空造出来
    navigator.languages = ['zh-CN','zh','en']  # 原生是 ['zh-CN']
    defineProperty(navigator,'webdriver')  # 会在 navigator 上留自有属性，可被
                                           # getOwnPropertyDescriptor 检出
这些"补指纹"把阿里云风险分推高了，是 F001 的嫌疑来源之一。

────────────────────────────────────────────────────────────────────
🔴 行为风控：F001 的真正判据
────────────────────────────────────────────────────────────────────
`VerifyCaptchaV3 -> F001` 是**行为风控拒绝**：captchaVerifyParam 里带的是
鼠标轨迹 + 按键节奏 + 设备指纹，轨迹太假就会被拒。
早期实现是 `move → 跳一步 → 立刻 down/up`，全程只有 2 个轨迹点、耗时 ~420ms，
真人不可能这样操作。现改为贝塞尔曲线分步移动（几十个点、随机步间延时）
+ 过冲回正 + 随机按压时长。

**关键推论（决定了本模块的性能优化方向）**：
既然风控看的是**轨迹与按键节奏**，那么"没有输入事件的固定等待"就是纯浪费 ——
它既不产生行为数据，又要占用时间。因此本模块用两类手段取代固定 sleep：
  - `wait_for_selector` / `wait_for`：等真实条件成立，需要多久等多久
  - 把鼠标微移动**塞进**原本空转的验证码初始化等待里（`_micro_move`）
但**打字延迟与鼠标步进延时必须保留** —— 那正是被评估的信号本身。

────────────────────────────────────────────────────────────────────
🔴 验证码有两条通路，`captcha_wait` 不是一个可以直接比较的数
────────────────────────────────────────────────────────────────────
早期我把"优化前 4s / 优化后 8.19s"当成回归，其实是**拿两个不同的通路在比**。
实测（`.workbuddy-ai/tmp/probe_captcha_timing.py`，微移动开/关各 3 轮，6/6 成功）：

| 组 | 通路 | captcha_wait | submit→jwt | 点击次数 |
|----|------|--------------|------------|----------|
| 微移动 ON | **Path A** TRACELESS 自过（T001） | 17.95 / 8.20 / 8.16 | 17.95 / 8.20 / 8.16 | **0** |
| 纯等待 OFF | **Path B** F001 → Init#2 → 点击 → T001 | 5.85 / 5.05 / 9.84 | 10.06 / 9.99 / — | 3 |

两条结论：

1. **微移动确实拉长了 SDK 的决策窗口**（均值 6.91s → 11.44s）——
   行为数据一直在更新，TRACELESS 就迟迟不认输、不降级。
   所以"8.19s 比 4s 慢"这个观察**方向是对的**，但归因错了：
   慢不是因为代码变差，是因为它走了另一条通路。
2. **但它换来"零交互"**：TRACELESS 自己通过，省掉整整一次点击
   （轨迹编排 2.6s + 验证往返 2.3s）。中位数反而更快（8.20s vs 10.0s）。
   代价是方差变大，出现过一次 17.95s。

因此 `_micro_move` **保留**（Path A 中位数更快 + 零交互，少一整个失败面），
`MICRO_BUDGET_S` 只作**病态兜底**。

⚠ **但"把预算调小、两头都要"这条路已被实测否定**（第二轮，各 3 轮）：

| 预算 | 通路 | submit→jwt | 点击 |
|------|------|------------|------|
| ∞ | A ×3 | 8.20 / 8.16 / 17.95 | 0 |
| 5.0s | B ×3 | 11.00 / 14.07 / — | 3 |
| 2.5s | B ×3 | ~15.4 / — / — | 3 |
| 0（关） | B ×3 | 10.06 / 9.99 / — | 3 |

原因：**喂数据会把 `Init#1` 推后**（`submit → Init#1` 从 5s 拉到 6.8s）。
中途停手 = 既付了推迟的代价、又拿不到 Path A 的免点击。
所以预算取值**必须在 Path A 中位数（~8.2s）之上**，否则等于主动放弃 Path A。

⚠ 由此得出的一条通用判据：**给验证码类流程计时，必须把"走了哪条通路"
   和"这条路花了多久"一起记录**（本模块记在 `captcha_stage.path`）。
   只记一个总时长，会把通路切换误读成性能回归。

────────────────────────────────────────────────────────────────────
✅ 无头模式可用（2026-09-15 实测推翻此前判断）
────────────────────────────────────────────────────────────────────
早期版本在文件头写过"headless 会被识别，必须 headful" —— 那是**推断，且是错的**。
在最小化注入 + 人类轨迹的实现下实测三组无头配置（`.workbuddy-ai/tmp/probe_headless.py`）：

| 配置 | 额外参数 | 结果 | 耗时 |
|------|----------|------|------|
| H1 基础无头 | 无 | ✅ `click #1 → T001` | 47s |
| H2 显式窗口尺寸 | `--window-size=1280,720 --force-device-scale-factor=1` | ✅ `click #1 → T001` | 37s |
| H3 软件 GPU | 再加 `--use-gl=angle --use-angle=swiftshader` | ✅ `click #1 → T001` | 31s |

**三组全部首次点击即通过。** 两个反直觉之处：

  - 无头模式下 UA 里**明写着 `HeadlessChrome/152.0.0.0`**，照样通过；
  - 无头模式下 `outerWidth == innerWidth == screen.width`（没有窗口边框），
    GPU renderer 也如实上报（H1/H2 是真实 NVIDIA RTX 3050，H3 是 SwiftShader），
    同样通过。

结论：阿里云这套风控**主要看行为轨迹，不是 UA / 窗口 / GPU 指纹**。
这也解释了为什么早期"随机化 UA + viewport"无效、而"人类鼠标轨迹"一次就过。

**唯一仍做的覆盖**：无头模式把 UA 里的 `HeadlessChrome` 归一化成 `Chrome`
（版本号原样保留）。这不是伪造指纹，而是**去掉一个无意义的自我标记**。

────────────────────────────────────────────────────────────────────
其它必须遵守的约束（踩坑记录）
────────────────────────────────────────────────────────────────────
1. **必须点击 `#aliyunCaptcha-checkbox-icon`**（20x20 真实图标），
   点外层 wrapper / body 都无效。
2. **必须等 SDK 切到 CHECK_BOX 再点**：判据是 `InitCaptchaV3` 出现第 2 次。
   第 1 次是 TRACELESS（无痕预检），此时 `#aliyunCaptcha-checkbox-icon`
   就已 visible，点击无效且不报错。注意第 1 次后的 `F001` 是**正常现象**。
3. **🔴 绝对不要用 `time.sleep()` 等待网络事件**。Playwright 同步 API
   只在调用其自身 API 时泵送事件循环；纯 `time.sleep()` 期间
   `page.on("response")` 回调不会执行，网络事件全部积压，
   表现为"等了 150 秒一个请求都没有，之后瞬间涌出 8 个"。
   必须用 `page.wait_for_timeout()`（它会泵送事件）。这是本项目最隐蔽的坑。
   —— 例外：**浏览器已关闭后**的重试冷却可以用 `time.sleep()`，此时没有事件循环。
4. **必须勾选协议复选框**：`#normal_login_autoLogin` 之外还有第二个
   checkbox（无 id，登录即代表同意协议），未勾选时提交不产生任何请求。
5. 登录入口路径：/login -> 「使用手机号 / 密码登录」-> 「密码登录」tab。
   默认落地页是微信扫码登录。
6. JWT 来源：login/byAccount 的响应头 `authorization: Bearer <jwt>`。
7. **不要覆盖 UA / viewport**（无头模式的 UA 归一化是唯一例外，见上）。
   `viewport=None` 让浏览器保持原样。
8. **F001 后不要在同一个浏览器里反复点**。同一会话已被打上风险标记，
   再点大概率继续 F001。正确做法是关掉浏览器、冷却、换新会话重来。
"""

import math
import os
import random
import time
from dataclasses import dataclass, field

from . import config

# ────────────────────────────────────────────────────────────────
# 🔬 对照实验开关（只影响 `_micro_move` 的行为，不影响生产默认值）
# ────────────────────────────────────────────────────────────────
# 置 IR_NO_MICRO_MOVE=1 时，`_micro_move` 退化成**纯等待**（不发任何鼠标事件）。
# 用途：测量"把鼠标微移动塞进验证码初始化等待"这一优化，究竟让
# `captcha_ready` 变快了还是变慢了 —— 单看一个粗粒度耗时标记无法归因。
MICRO_MOVE = os.getenv("IR_NO_MICRO_MOVE", "").strip().lower() not in ("1", "true", "yes")

# 对照组每次 `_micro_move` 的等待时长（毫秒），与实验组单次耗时同量级，
# 保证两组在"循环转了几圈"上可比。
MICRO_WAIT_MS = 260

# ─────────────────────────────────────────────────────────────────
# 🔴 微移动预算（秒）—— 2026-09-15 两轮对照实验的产物
# ─────────────────────────────────────────────────────────────────
# 【实验一】喂不喂数据（各 3 轮，6/6 成功）
#
#   微移动 ON ：3/3 走 TRACELESS **直接通过**（0 次点击）
#                captcha_wait = 17.95 / 8.20 / 8.16 s
#   纯等待 OFF：3/3 走 F001 → Init#2 → 点击 → T001
#                captcha_wait =  5.85 / 5.05 / 9.84 s，但每次多付一次点击
#                （轨迹编排 2.6s + 验证往返 2.3s）
#
# 两条结论：
#   1. 微移动**确实拉长了 SDK 的决策窗口**（6.91s → 11.44s）——
#      行为数据一直在更新，TRACELESS 不肯认输、迟迟不降级。
#   2. 但它换来"零交互"：TRACELESS 自己通过，省掉点击，中位数反而更快
#      （8.20s vs 10.0s）。代价是方差变大（出现过一次 17.95s）。
#
# 【实验二】把预算调小能不能"两头都要"？—— 不能，实测是**所有组合里最差的**
#
#   预算    通路   submit→jwt（各次观测）                       点击
#   ∞       A     6.66 / 7.64 / 8.20 / 8.16 / 8.86 / 17.95       0
#                 → 中位 8.2s，均值 9.6s
#   12.0s   B     23.54                                           3
#   5.0s    B     11.00 / 14.07 / —                               3
#   2.5s    B     ~15.4 / — / —                                   3
#   0(关)   B      9.99 / 10.06 / —                               3
#                 → 中位 11.0s，均值 13.4s
#
# 原因有两层：
#   1. 喂数据会把 `Init#1` **推后**（submit→Init#1 从 5s 拉到 6.8s）；
#   2. 降级后的点击开销**并不会因为我们提前停手而变小**
#      （实测 click+verify 稳定要 4.9~6.4s）。
#   → 中途停手 = 既付了推迟的代价、又拿不到 Path A 的免点击，**两头都亏**。
#
# 【结论】预算**只能当病态兜底，取值必须高到实践中永不触发**。
# 45s 的定位：远高于实测 Path A 上界（17.95s），只有 SDK 真卡死时才触发，
# 把最坏情况从 `login(timeout=150)` 的 150s 压到 ~60s。
# ⚠ 不要把它当成"性能旋钮"往下调 —— 那样只会稳定地拿到更差的 Path B。
MICRO_BUDGET_S = float(os.getenv("IR_MICRO_BUDGET", "45"))

# ─────────────────────────────────────────────────────────────────
# 逐字输入的按键间隔（毫秒）—— 已实测：**调小净收益仅 0.5s，别动**
# ─────────────────────────────────────────────────────────────────
# 文件头原写"打字延迟是行为信号，不要调小"。那是推断。
#
# 🔴 这个结论在本会话里被**推翻过两次**，教训比结论本身值钱：
#
#   轮次   样本         typed        captcha_wait   登录总耗时     结论
#   最初   各 3 轮      4.87→2.94    5.37→7.26      16.56→16.58   ≈ 0
#   中途   各 6 轮      4.64→2.61    7.04→6.30      16.37→13.53   省 2.84s（差点改默认值）
#   最终   合并 9/8     5.26→2.72    5.78→6.41      15.39→14.89   省 0.50s
#
# 根因：`captcha_wait` 方差极大（实测 3.85 ~ **19.73s**），单轮噪声能轻易
# 淹没 2s 级别的真实差异。两个必做动作：
#   ① 丢弃第 1 轮（冷启动，captcha_wait 恒偏高：实测 6.04s / 7.30s）
#   ② 合并多个实验文件后**比中位数**，不要拿单次运行的均值下结论
#
# → 所以**默认值保持 45~110ms**（更接近真人，且几乎没有性能代价）。
#   这两个开关留着是为了将来重测，不是为了当性能旋钮。
#   复现：tools/probe_login_timing.py --type-lo 15 --type-hi 40 --drop-first
TYPE_DELAY_LO = int(os.getenv("IR_TYPE_DELAY_LO", "45"))
TYPE_DELAY_HI = int(os.getenv("IR_TYPE_DELAY_HI", "110"))

# ─────────────────────────────────────────────────────────────────
# 🔬 预加载实验开关（2026-09-15）—— **假设已被否定**，开关留作复现
# ─────────────────────────────────────────────────────────────────
# 【当初的假设】
#   `goto`+`form_ready` = 3.57s，`typed`+`warmup` = 6.29s，合计才 9.86s，
#   之后却还要再等 `captcha_ready` 7.14s（总 17.0s）—— 看起来像存在一个
#   "按页面加载时刻起算的固定窗口"。推论：可以在处理上一个账号时
#   **并行预加载**下一个页面，把这段等待吃掉。
#
# 【实测结果：不成立】
#   在 `goto` 之后闲置 15s 再操作，`captcha_wait` 不但没缩短，反而略增：
#
#     组                     captcha_wait 中位
#     页面加载后立即操作        4.89s
#     页面加载后闲置 15s        5.55s     ← 总耗时白涨 13s
#
#   → 窗口**不是**"页面加载后计时"，预加载策略无效。
#
# 【真正的原因】方差来自 SDK 内部，与页面加载多久无关：
#     submit → Init#1     中位 ~2.0s   实测范围  1.78 ~ 17.50s
#     Init#1 → Verify#1   中位 ~5.0s   实测范围  3.41 ~ 19.03s
#   两者我们都控制不到 —— 所以单账号登录耗时压不动（实测 11.1 ~ 26.2s），
#   只能靠**并发吸收方差**（见 README「workers 的边界」）。
#
# ⚠ 开关保留用于复现：tools/probe_login_timing.py --prewarm 15000
#   默认 0 = 完全不改变生产行为。
PREWARM_MS = int(os.getenv("IR_PREWARM_MS", "0"))

CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-features=IsolateOrigins,site-per-process",
]

# 见文件头「最小化注入」说明：这里只做一件无害的事。
ANTI_DETECT_JS = """
if (!window.chrome) { window.chrome = { runtime: {} }; }
"""

MAX_CLICKS_PER_ATTEMPT = 3

# 页面元素就绪的等待上限（自适应等待，不是固定 sleep）
UI_WAIT_MS = 15000


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
# 人类化鼠标动作
# ────────────────────────────────────────────────────────────────
def _human_move(page, x0: float, y0: float, x1: float, y1: float,
                *, steps: int = None) -> None:
    """沿三次贝塞尔曲线分步移动鼠标，模拟人类轨迹。

    真人移动的特征：不是直线、有轻微弧度、速度先快后慢（ease-out）、
    步间间隔不均匀。这里用 smoothstep 缓动 + 随机控制点偏移还原。

    ⚠ 步间延时（9~30ms）**不要去掉** —— 它是被风控评估的信号本身。
    """
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    if dist < 1.5:
        page.mouse.move(x1, y1)
        return
    if steps is None:
        steps = max(10, min(70, int(dist / 7)))
    amp = min(55.0, dist * 0.18)
    c1x = x0 + dx * 0.30 + random.uniform(-amp, amp)
    c1y = y0 + dy * 0.30 + random.uniform(-amp, amp)
    c2x = x0 + dx * 0.70 + random.uniform(-amp, amp)
    c2y = y0 + dy * 0.70 + random.uniform(-amp, amp)
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)          # smoothstep 缓动
        mt = 1 - e
        x = (mt ** 3 * x0 + 3 * mt * mt * e * c1x
             + 3 * mt * e * e * c2x + e ** 3 * x1)
        y = (mt ** 3 * y0 + 3 * mt * mt * e * c1y
             + 3 * mt * e * e * c2y + e ** 3 * y1)
        page.mouse.move(x, y)
        page.wait_for_timeout(random.randint(9, 30))


def _idle_wait(page, *, stats: dict = None) -> None:
    """**不发任何输入事件**，只泵送事件循环地等一小段。

    两个用途，语义相同（都是"让页面安静下来"）：
      1. 对照组（IR_NO_MICRO_MOVE=1）—— 隔离出"鼠标事件"这一个变量；
      2. 超过 `MICRO_BUDGET_S` 之后的安静期 —— 停止喂数据，
         给 SDK 一个不再被新轨迹延后的窗口去下降级决定。

    ⚠ 仍然必须用 `wait_for_timeout` 而不是 `time.sleep`（见文件头第 3 条）：
      安静期里恰恰最需要网络事件回调被执行。
    """
    if stats is not None:
        stats["idle_waits"] = stats.get("idle_waits", 0) + 1
    page.wait_for_timeout(MICRO_WAIT_MS)


def _micro_move(page, cur: tuple, vw: int, vh: int, *, stats: dict = None) -> tuple:
    """做一次小幅鼠标移动，返回新位置。

    用途：在"等验证码 SDK 初始化"这类原本空转的等待里持续产生行为数据。
    每次约 150~350ms，比整段 warmup 更贴近真人（真人不会停手不动）。

    对照组（IR_NO_MICRO_MOVE=1）：只等同样长的时间，**不发鼠标事件**。
    这是为了回答一个具体问题 —— 微移动到底加快了还是拖慢了 SDK 的
    TRACELESS→CHECK_BOX 降级时序。

    统计口径（`stats`）刻意把三种动作分开计数，否则"移动次数"会把
    纯等待也算进去，实验组和对照组就没法比：
      moves      真正发出了鼠标轨迹的微移动次数
      points     累计发出的轨迹点数
      idle_waits 未发事件的纯等待次数（对照组 + 超预算后的安静期）
    """
    if not MICRO_MOVE:
        _idle_wait(page, stats=stats)
        return cur
    x = min(max(cur[0] + random.uniform(-170, 170), 20), max(vw - 20, 21))
    y = min(max(cur[1] + random.uniform(-120, 120), 20), max(vh - 20, 21))
    steps = random.randint(5, 13)
    _human_move(page, cur[0], cur[1], x, y, steps=steps)
    if stats is not None:
        stats["moves"] = stats.get("moves", 0) + 1
        stats["points"] = stats.get("points", 0) + steps
    page.wait_for_timeout(random.randint(50, 150))
    return (x, y)


def _warmup_mouse(page, vw: int, vh: int, *, stats: dict = None) -> tuple:
    """提交前的短暖场（2~3 轮）。

    真正的长时间鼠标活动交给 `_micro_move` 在验证码初始化等待期间做 ——
    那样不额外占用时间。这里只保证"提交动作前手是动过的"。
    """
    x = random.uniform(vw * 0.25, vw * 0.75)
    y = random.uniform(vh * 0.25, vh * 0.60)
    page.mouse.move(x, y)
    page.wait_for_timeout(random.randint(120, 260))
    for _ in range(random.randint(2, 3)):
        x, y = _micro_move(page, (x, y), vw, vh, stats=stats)
    return x, y


def _click_checkbox(page, cur: tuple) -> tuple:
    """移动到 `#aliyunCaptcha-checkbox-icon` 中心并按下。

    返回 (新光标位置, 是否成功发出点击)。
    """
    icon = page.locator("#aliyunCaptcha-checkbox-icon").first
    box = icon.bounding_box()
    if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
        return cur, False
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2

    # 先移到附近（带过冲），停顿，再校正到中心 —— 人类常见动作模式
    nx = cx + random.uniform(-45, 45)
    ny = cy + random.uniform(-30, 30)
    _human_move(page, cur[0], cur[1], nx, ny)
    page.wait_for_timeout(random.randint(150, 380))
    _human_move(page, nx, ny, cx, cy, steps=random.randint(6, 14))
    page.wait_for_timeout(random.randint(90, 260))

    page.mouse.down()
    page.wait_for_timeout(random.randint(70, 170))
    page.mouse.up()
    return (cx, cy), True


def _has_slider(page) -> str:
    """检测点击后是否弹出了滑块 / 拼图二次验证。"""
    try:
        return page.evaluate("""() => {
            const ids = ['aliyunCaptcha-sliding', 'aliyunCaptcha-puzzle',
                         'aliyunCaptcha-slider', 'aliyunCaptcha-slide'];
            for (const i of ids) {
                const el = document.getElementById(i);
                if (el && el.getBoundingClientRect().width > 0) return i;
            }
            const all = document.querySelectorAll('[id*="Captcha"]');
            for (const el of all) {
                const r = el.getBoundingClientRect();
                if (r.width > 100 && /slid|puzzle|slide/i.test(el.id)) return el.id;
            }
            return '';
        }""")
    except Exception:
        return ""


def build_login_url() -> str:
    redirect = (
        f"{config.DISCOVERY_BASE}/token-plan/home?tabIndex=0"
        f"&clientId={config.CLIENT_ID}&source={config.SOURCE}"
    )
    return f"{config.SSO_BASE}/login?redirect={redirect}"


# ────────────────────────────────────────────────────────────────
# 在给定 browser 上跑一次尝试（自带 context 生命周期）
# ────────────────────────────────────────────────────────────────
def _run_attempt(browser, *, account: str, password: str, headless: bool,
                 timeout: int, screenshot_prefix: str = None,
                 verbose: bool = False, tag: str = "") -> LoginResult:
    jwt_holder = {"jwt": ""}
    code_holder = {"code": ""}
    cap = {"init": 0, "verify": [], "last_ok": False, "payload": "",
           "slider": "", "trivial": 0, "events": []}
    mv = {}          # 鼠标动作统计：moves=微移动次数, points=发出的轨迹点数
    tm = {}
    t_start = time.time()

    def mark(name: str):
        tm[name] = round((time.time() - t_start) * 1000)

    def ev(kind: str, detail: str = ""):
        """细粒度事件时间线。

        只有把 InitCaptchaV3 / VerifyCaptchaV3 的**到达时刻**逐个记下来，
        才能回答"captcha_ready 这 8 秒到底花在哪一段" —— 单个粗粒度标记
        做不到归因，只能看到总时长变了却不知道谁变了。
        """
        cap["events"].append([round((time.time() - t_start) * 1000), kind, detail])

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

    def on_response(resp):
        try:
            hdrs = dict(resp.headers)
        except Exception:
            hdrs = {}
        auth = hdrs.get("authorization", "")
        if auth.startswith("Bearer ") and not jwt_holder["jwt"]:
            jwt_holder["jwt"] = auth[len("Bearer "):]
        u = resp.url
        if "internal/auth" in u and not code_holder["code"]:
            try:
                code_holder["code"] = (resp.json().get("data") or {}).get("code", "")
            except Exception:
                pass
        try:
            pd = resp.request.post_data or ""
        except Exception:
            pd = ""
        if "InitCaptchaV3" in pd:
            cap["init"] += 1
            ev(f"Init#{cap['init']}")
            if verbose:
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
                ev(f"Verify#{len(cap['verify'])}", f"{vc} ok={ok}")
                if verbose:
                    print(f"    [login] VerifyCaptchaV3 -> {vc} ok={ok}", flush=True)
            except Exception:
                pass

    page.on("response", on_response)

    try:
        page.goto(build_login_url(), wait_until="domcontentloaded", timeout=60000)
        mark("goto")

        # 🔬 预加载实验：页面加载后先闲置一段再操作（原理见 PREWARM_MS）。
        #    默认 0，不进这个分支 —— 生产行为与实验前完全一致。
        if PREWARM_MS > 0:
            page.wait_for_timeout(PREWARM_MS)
            mark("prewarm")

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
        mark("form_ready")

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
        mark("typed")

        # 4) 勾选全部协议类复选框
        for i in range(page.locator("input[type=checkbox]").count()):
            el = page.locator("input[type=checkbox]").nth(i)
            try:
                if not el.is_checked():
                    el.check(timeout=5000)
            except Exception:
                pass
        mark("checkbox")

        # 5) 短暖场（2~3 轮）。长时间鼠标活动改在下面等验证码时做
        vw, vh = page.evaluate("[window.innerWidth, window.innerHeight]")
        cur = _warmup_mouse(page, vw, vh, stats=mv)
        mark("warmup")

        if screenshot_prefix:
            page.screenshot(path=f"{screenshot_prefix}{tag}_filled.png")

        # 6) 提交
        page.locator("button").filter(has_text="登录").first.click(timeout=10000)
        ev("submit")

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
            if jwt_holder["jwt"]:
                break
            if (time.time() - t_cap) < MICRO_BUDGET_S:
                cur = _micro_move(page, cur, vw, vh, stats=mv)
            else:
                if not mv.get("budget_logged"):
                    mv["budget_logged"] = True
                    ev("micro_budget_exhausted", f"{MICRO_BUDGET_S}s")
                    if verbose:
                        print(f"    [login] 微移动预算用尽（{MICRO_BUDGET_S}s），"
                              f"转入安静期等 SDK 降级", flush=True)
                _idle_wait(page, stats=mv)
        waited = round((time.time() - t_cap) * 1000)
        mark("captcha_ready")
        ev("captcha_ready", f"waited={waited}ms init={cap['init']}")
        if verbose:
            print(f"    [login] init count = {cap['init']} "
                  f"(waited {waited / 1000:.1f}s, traceless_reject={cap['trivial']}, "
                  f"micro_moves={mv.get('moves', 0)})", flush=True)

        # 8) 点击复选框，最多 MAX_CLICKS_PER_ATTEMPT 轮
        for attempt in range(MAX_CLICKS_PER_ATTEMPT):
            if jwt_holder["jwt"]:
                break
            cur, clicked = _click_checkbox(page, cur)
            ev(f"click#{attempt + 1}", f"clicked={clicked}")
            if verbose:
                print(f"    [login] click #{attempt + 1} at "
                      f"({cur[0]:.0f},{cur[1]:.0f}) clicked={clicked}", flush=True)
            if not clicked:
                page.wait_for_timeout(1200)
                continue

            # 等本轮结果（必须 pump 事件）。同时继续做小幅移动，
            # 让"正在等待"这件事本身也表现为真人行为。
            for i in range(10):
                page.wait_for_timeout(1000)
                if jwt_holder["jwt"]:
                    break
                cur = _micro_move(page, cur, vw, vh, stats=mv)
            if jwt_holder["jwt"]:
                break
            sl = _has_slider(page)
            if sl:
                cap["slider"] = sl
                ev("slider", sl)
                if verbose:
                    print(f"    [login] ⚠ 弹出二次验证: {sl}", flush=True)
                break
            page.wait_for_timeout(random.randint(400, 900))
        mark("done")
        if jwt_holder["jwt"]:
            ev("jwt")

        if screenshot_prefix:
            page.screenshot(path=f"{screenshot_prefix}{tag}_done.png")

        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        jwt = jwt_holder["jwt"] or cookies.get("uaa-token", "")
        reason = ""
        if not jwt:
            if cap["slider"]:
                reason = f"secondary captcha required ({cap['slider']})"
            elif cap["verify"]:
                reason = f"captcha rejected ({cap['verify'][-1]})"
            else:
                reason = "no jwt captured"
        return LoginResult(
            ok=bool(jwt), jwt=jwt, code=code_holder["code"], reason=reason,
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
                                     "moves": mv.get("moves", 0),
                                     "points": mv.get("points", 0),
                                     "idle_waits": mv.get("idle_waits", 0),
                                     "budget_exhausted": bool(
                                         mv.get("budget_logged"))},
                           "captcha_wait_ms": waited},
            timings=tm,
        )
    except Exception as ex:
        try:
            if screenshot_prefix:
                page.screenshot(path=f"{screenshot_prefix}{tag}_error.png")
        except Exception:
            pass
        return LoginResult(ok=False, reason=f"{type(ex).__name__}: {ex}"[:200],
                           captcha_stage={"init": cap["init"],
                                          "verify": cap["verify"],
                                          "events": cap["events"]},
                           timings=tm)
    finally:
        try:
            ctx.close()
        except Exception:
            pass


# ────────────────────────────────────────────────────────────────
# 浏览器复用会话
# ────────────────────────────────────────────────────────────────
class BrowserSession:
    """复用一个浏览器进程，每个账号开一个全新 context。

    Chrome 冷启动约 1.5~2s，批量场景下这笔开销乘以账号数。
    复用浏览器只换 context（cookie / localStorage / 缓存全新建），
    会话隔离性与新开浏览器等价 —— 风控看到的是新会话，不是新进程。

    用法：
        with BrowserSession(headless=True) as sess:
            for acct in accounts:
                res = sess.login(acct.email, acct.password, verbose=True)

    ⚠ Playwright 同步 API 不能跨线程共享。多线程并发时，
      **每个线程各自建一个 BrowserSession**。
    """

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._pw = None
        self._browser = None
        self.launch_ms = 0

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        t0 = time.time()
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            executable_path=config.CHROME_PATH, headless=self.headless,
            args=CHROME_ARGS,
        )
        self.launch_ms = round((time.time() - t0) * 1000)
        return self

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()
        return False

    def login(self, account: str, password: str, *, timeout: int = 150,
              attempts: int = 3, cooldown: float = 15.0,
              screenshot_prefix: str = None, verbose: bool = False) -> LoginResult:
        """在复用的浏览器上登录，失败换新 context 重试。"""
        last = LoginResult(ok=False, reason="not attempted")
        for i in range(max(1, attempts)):
            tag = f"_a{i + 1}"
            if verbose and attempts > 1:
                print(f"    [login] === 尝试 {i + 1}/{attempts} ===", flush=True)
            res = _run_attempt(self._browser, account=account, password=password,
                               headless=self.headless, timeout=timeout,
                               screenshot_prefix=screenshot_prefix,
                               verbose=verbose, tag=tag)
            res.attempts_used = i + 1
            if res.ok:
                return res
            last = res
            if i < attempts - 1:
                # 这里浏览器还开着，不能 time.sleep（会阻塞事件循环），
                # 但已经不需要等网络事件，用 wait_for_timeout 安全。
                wait = cooldown * (i + 1) + random.uniform(0, 5)
                if verbose:
                    print(f"    [login] 第 {i + 1} 次失败（{res.reason}），"
                          f"冷却 {wait:.0f}s 后重试", flush=True)
                time.sleep(wait)
        return last


# ────────────────────────────────────────────────────────────────
# 单账号入口（自带浏览器生命周期）
# ────────────────────────────────────────────────────────────────
def login(account: str, password: str, *, headless: bool = False,
          timeout: int = 150, attempts: int = 3, cooldown: float = 15.0,
          screenshot_prefix: str = None, verbose: bool = False) -> LoginResult:
    """用真实浏览器登录 SSO，返回 JWT。

    单账号场景用这个；批量场景请用 `BrowserSession` 复用浏览器进程。

    Args:
        account: 邮箱 / 手机号 / 用户名
        password: 明文密码
        headless: 无头模式。**实测可用**（3/3 通过，见文件头说明）
        timeout: 单次尝试里验证码阶段的等待上限（秒）
        attempts: 失败后换新会话重试的次数
        cooldown: 两次尝试之间的冷却基数（秒），按次数线性递增
        screenshot_prefix: 若提供，保存过程截图便于排查
        verbose: 打印验证码交互细节

    Returns:
        LoginResult；`attempts_used` 记录实际用掉几次尝试，`timings` 是各阶段耗时(ms)。
    """
    from playwright.sync_api import sync_playwright

    last = LoginResult(ok=False, reason="not attempted")
    for i in range(max(1, attempts)):
        tag = f"_a{i + 1}"
        if verbose and attempts > 1:
            print(f"    [login] === 尝试 {i + 1}/{attempts} ===", flush=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=config.CHROME_PATH, headless=headless, args=CHROME_ARGS
            )
            try:
                res = _run_attempt(browser, account=account, password=password,
                                   headless=headless, timeout=timeout,
                                   screenshot_prefix=screenshot_prefix,
                                   verbose=verbose, tag=tag)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
        res.attempts_used = i + 1
        if res.ok:
            return res
        last = res
        if i < attempts - 1:
            # 此时浏览器已关闭，没有事件循环要泵送，sleep 是安全的
            wait = cooldown * (i + 1) + random.uniform(0, 5)
            if verbose:
                print(f"    [login] 第 {i + 1} 次失败（{res.reason}），"
                      f"冷却 {wait:.0f}s 后换新会话重试", flush=True)
            time.sleep(wait)
    return last
