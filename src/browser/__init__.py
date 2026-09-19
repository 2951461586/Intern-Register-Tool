"""浏览器登录模块 —— 通过本机 Chrome 完成 OpenXLab SSO 登录并提取 JWT。

为什么需要浏览器：
  `login/byAccount` 强制阿里云验证码 2.0（错误码 B0501「人机验证失败」），
  该验证码的 captchaVerifyParam 依赖设备指纹与阿里云签发的 securityToken，
  纯 HTTP 无法伪造（DeviceToken 末段是服务端签名）。实测所有登录入口
  （byAccount / byPhone / getSmsCode）都要人机验证，因此必须借助真实浏览器。

────────────────────────────────────────────────────────────────────
🔴 反检测策略：**最小化注入**（2026-09-15 实测修正）
────────────────────────────────────────────────────────────────────
本机真实 Chrome 为 152.0.7977.83。实测（见 tools/probes/probe_env.py）：
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
实测（`tools/probes/probe_captcha_timing.py`，微移动开/关各 3 轮，6/6 成功）：

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
在最小化注入 + 人类轨迹的实现下实测三组无头配置（`tools/probes/probe_headless.py`）：

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
from .state import LoginResult
from .urls import build_login_url

__all__ = [
    # 公共 API
    "login",
    "BrowserSession",
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
