"""浏览器登录模块 —— 通过本机 Chrome 完成 OpenXLab SSO 登录并提取 JWT。

为什么必须用浏览器：
  `login/byAccount` 强制阿里云验证码 2.0（错误码 B0501「人机验证失败」），
  `captchaVerifyParam` 依赖设备指纹与阿里云签发的 securityToken，纯 HTTP 无法伪造。
  实测 `byAccount` / `byPhone` / `getSmsCode` 三个入口都要人机验证。

────────────────────────────────────────────────────────────────────
🔴 反检测策略：**最小化注入**（2026-09-15 实测修正）
────────────────────────────────────────────────────────────────────
真实 Chrome 加 `--disable-blink-features=AutomationControlled` 之后，
`navigator.webdriver` / `plugins` / `window.chrome` **原生就是干净的**。
⇒ **绝对不要再去"补"这些属性**。早期版本注入的下面这些写法反而在制造破绽：

    navigator.plugins = [1,2,3,4,5]           # 类型从 PluginArray 变普通 Array
    navigator.hardwareConcurrency = 8         # 真值 12 被改小
    navigator.deviceMemory = 8                # 原生是 undefined，凭空造出来
    defineProperty(navigator, 'webdriver')    # 留下自有属性，可被检出

这些"补指纹"把阿里云风险分推高了，是 F001 的嫌疑来源之一。
**唯一保留的覆盖**：无头模式把 UA 里的 `HeadlessChrome` 归一化成 `Chrome`
（版本号原样保留）—— 这不是伪造指纹，是去掉一个无意义的自我标记。

────────────────────────────────────────────────────────────────────
🔴 行为风控：F001 的真正判据，以及本模块的性能取向
────────────────────────────────────────────────────────────────────
`F001` 是**行为风控拒绝** —— `captchaVerifyParam` 里带的是鼠标轨迹 + 按键节奏 +
设备指纹，轨迹太假就被拒（早期实现全程只有 2 个轨迹点、耗时 ~420ms）。
因此本模块：

  · 鼠标走贝塞尔分步 + 过冲回正 + 随机按压时长（见 `behavior.py`）；
  · **"没有输入事件的固定等待"是纯浪费**（既不产生行为数据、又占时间）⇒
    用 `wait_for_selector` 等真实条件，并把鼠标微移动**塞进**验证码初始化等待；
  · ⚠ **打字延迟与鼠标步进延时必须保留** —— 那正是被评估的信号本身。

⚠ `captcha_wait` **不是一个可以直接比较的数**：验证码有两条通路
（Path A = TRACELESS 自过、零点击；Path B = 降级后点击复选框）。
拿两条通路互比会把**通路切换误读成性能回归**。
⇒ 计时必须把「走了哪条通路」和「这条路花了多久」**一起记**
（本模块记在 `captcha_stage.path`）。

────────────────────────────────────────────────────────────────────
其它必须遵守的约束（踩坑记录）
────────────────────────────────────────────────────────────────────
1. **必须点击 `#aliyunCaptcha-checkbox-icon`**（20x20 真实图标），
   点外层 wrapper / body 都无效。
2. **必须等 SDK 切到 CHECK_BOX 再点**：判据是 `InitCaptchaV3` 出现第 2 次。
   第 1 次是 TRACELESS 预检，此时复选框就已 visible，点击无效且不报错；
   **第 1 次之后的 `F001` 是正常现象**，真正的失败判据是**点击后仍返回 `F001`**。
3. **🔴 绝对不要用 `time.sleep()` 等待网络事件**。Playwright 同步 API 只在调用
   其自身 API 时泵送事件循环；`time.sleep()` 期间 `page.on("response")` 回调
   不会执行，网络事件全部积压，表现为"等了 150 秒一个请求都没有，之后瞬间涌出
   8 个"。必须用 `page.wait_for_timeout()`（它会泵送事件）。
   **这是本项目最隐蔽的坑。**
   —— 例外：**浏览器已关闭后**的重试冷却可以用 `time.sleep()`，此时没有事件循环。
4. **必须勾选协议复选框**：`#normal_login_autoLogin` 之外还有第二个
   checkbox（无 id，登录即代表同意协议），未勾选时提交不产生任何请求。
5. 登录入口路径：/login -> 「使用手机号 / 密码登录」-> 「密码登录」tab。
   默认落地页是微信扫码登录。
6. **F001 后不要在同一个浏览器里反复点**：同一会话已被打上风险标记，再点大概率
   继续 F001。正确做法是关掉浏览器、冷却、换新会话重来。
7. **不要覆盖 UA / viewport**（无头模式的 UA 归一化是唯一例外，见上）。
   `viewport=None` 让浏览器保持原样。
8. JWT 来源：`login/byAccount` 的响应头 `authorization: Bearer <jwt>`。

────────────────────────────────────────────────────────────────────
📄 实测依据（对照实验表格、负结果、无头三组配置）见 `docs/protocol.md`：
   「浏览器登录：反检测要少做」/「点击验证码：行为风控看的是轨迹」/
   「验证码有两条通路」/「其它三个必要条件」/「无头浏览器：实测可用」。
"""

from .constants import (
    ANTI_DETECT_JS,
    CHROME_ARGS,
    MAX_CLICKS_PER_ATTEMPT,
    MICRO_BUDGET_S,
    MICRO_MOVE,
    MICRO_WAIT_MS,
    PREWARM_MS,
    TYPE_DELAY_HI,
    TYPE_DELAY_LO,
    UI_WAIT_MS,
)
from .entry import login
from .session import BrowserSession
from .settings import BrowserSettings
from .state import LoginResult
from .urls import build_login_url

__all__ = [
    # 公共 API
    "login",
    "BrowserSession",
    "BrowserSettings",
    "LoginResult",
    "build_login_url",
    # 可调常量（探针读取；全部可用环境变量覆盖，见 constants.py）
    "ANTI_DETECT_JS",
    "CHROME_ARGS",
    "MAX_CLICKS_PER_ATTEMPT",
    "MICRO_BUDGET_S",
    "MICRO_MOVE",
    "MICRO_WAIT_MS",
    "PREWARM_MS",
    "TYPE_DELAY_HI",
    "TYPE_DELAY_LO",
    "UI_WAIT_MS",
]
