# 协议要点

> 📄 本文件由 `README.md` 的「协议要点」整段迁出（2026-09-20，B8）。
> README 只保留「怎么用 + 架构 + 已知限制」；**协议与实测细节集中在本文件**，
> 避免同一份知识在 README 与源码 docstring 两处各写一遍、然后各自漂移。
> 文中的实测依据多来自 `tools/probes/`，探针索引见 `tools/probes/README.md`。
>
> **2026-10-06 起再按主题拆出两份**：邮箱源 → [`mailbox.md`](mailbox.md)；
> 注册链路的限流·配额·吞吐·耗时 → [`registration-limits.md`](registration-limits.md)。

## 密码加密（最容易踩的坑）

前端 `main.59963db7.chunk.js` 的真实实现：

```javascript
a.setPublicKey(pubKey);
a.encrypt(email + "||" + password + Math.floor(Date.now() / 1e3))
```

即明文为 `f"{identity}||{password}{unix_seconds}"`，再做 **RSA/ECB/PKCS1Padding**，最后 base64。

- ❌ 只加密 `password` → 服务端报 `A0216 用户密码解密失败`
- ✅ 带 `identity||` 前缀与秒级时间戳 → 成功
- 三种场景的 identity：注册用 `email`，登录用 `account`，改密用 `email`

## 人机验证（核心难点）

阿里云验证码 2.0，`prefix=lvtb1n`，`sceneId=8eq3rdkp`。

| 入口 | 是否强制验证码 |
|------|----------------|
| `register/byEmail` | ❌ 不需要 |
| `register/active` | ❌ 不需要 |
| `login/byAccount` | ✅ **强制**（`B0501 人机验证失败`） |
| `login/byPhone` | ✅ 强制 |
| `login/getSmsCode` | ✅ 强制 |
| `internal/auth` | 需登录态（`A0202`） |

验证码有**两条通路**（实测都会出现，见下节「验证码有两条通路」）：

```
Path A（免点击）  InitCaptchaV3 #1 ─ TRACELESS 预检直接通过 T001 ─▶ 拿到 JWT
Path B（降级）    InitCaptchaV3 #1 ─ TRACELESS 预检 F001
                 ─ 带 DeviceToken 重新 InitCaptchaV3 #2（CaptchaType=CHECK_BOX）
                 ─ 点击复选框 ─ VerifyCaptchaV3 T001 ─▶ 拿到 JWT
```

> **注意**：第一次 `F001` 是**正常现象**，不是失败 —— 它只是说明走 Path B。
> 真正的失败判据是**点击复选框后仍返回 `F001`**。

`captchaVerifyParam` 依赖设备指纹与阿里云签发的 `securityToken`，**纯 HTTP 无法伪造**，
故登录必须借助真实浏览器。

## 浏览器登录：反检测要"少做"而不是"多做"

🔴 **这是本项目最反直觉的一条。**

本机真实 Chrome 为 `152.0.7977.83`。实测（`tools/probes/probe_env.py`）：
只加 `--disable-blink-features=AutomationControlled` 时，Chrome **原生**就已经是

```
navigator.webdriver === false
navigator.plugins    → 长度 5 的真 PluginArray
window.chrome        → 存在
Object.keys(window)  → 无 cdc_/selenium/webdriver 残留
```

因此**不要去"补"这些属性**。早期版本注入的下面这些写法反而在制造破绽：

| 早期写法 | 为什么是破绽 |
|----------|--------------|
| `navigator.plugins = [1,2,3,4,5]` | 类型从 `PluginArray` 变成普通 `Array`；`plugins[0].name` 为 `undefined`，一眼假 |
| `navigator.permissions.query = ...` | 返回普通对象，不是 `PermissionStatus` |
| `navigator.hardwareConcurrency = 8` | 真值 12 被改小 |
| `navigator.deviceMemory = 8` | 原生是 `undefined`，凭空造出来 |
| `navigator.languages = ['zh-CN','zh','en']` | 原生就是 `['zh-CN']` |
| `defineProperty(navigator,'webdriver')` | 会在 `navigator` 上留自有属性，可被 `getOwnPropertyDescriptor` 检出 |

同理，**不要覆盖 UA 和 viewport**：

- 真实 Chrome 的 UA 已经是 `Chrome/152.0.0.0`（Chrome 自 101 起用 reduced UA）。
  自己拼 `149/150/151` 会和 `navigator.userAgentData` 报的真实版本打架。
- `new_context(viewport=...)` 走的是 CDP `Emulation.setDeviceMetricsOverride`，
  会把 `screen.width/height` 一起改掉，留下与真实显示器不一致的痕迹。
  用 `viewport=None` 让浏览器保持原样。

**当前实现只注入一行**（`window.chrome` 兜底），其余全部交给 Chrome 原生行为。

## 🔴 验证码有两条通路：`captcha_wait` 不是一个能直接比较的数

**这是本项目最容易误判的一条。** 早期我把"优化前 4s / 优化后 8.19s"当成性能回归，
其实是**拿两条不同的通路在比**。

对照实验（`tools/probes/probe_captcha_timing.py`，各 3 轮，6/6 成功）：

| 组 | 通路 | `captcha_wait` | `submit → jwt` | 点击次数 |
|----|------|----------------|----------------|----------|
| 微移动 ON | **Path A** TRACELESS 自过（`T001`） | 17.95 / 8.20 / 8.16 s | 17.95 / 8.20 / 8.16 s | **0** |
| 纯等待 OFF | **Path B** `F001` → `Init#2` → 点击 → `T001` | 5.85 / 5.05 / 9.84 s | 10.06 / 9.99 / — s | 3 |

两条结论：

1. **鼠标微移动确实拉长了 SDK 的决策窗口**（均值 6.91s → 11.44s）——
   行为数据一直在更新，TRACELESS 就不肯认输、迟迟不降级。
   所以"变慢了"这个观察**方向是对的，但归因错了**：慢不是因为代码变差，
   是因为它走了另一条通路。
2. **但它换来"零交互"**：TRACELESS 自己通过，省掉整整一次点击
   （轨迹编排 2.6s + 验证往返 2.3s）。中位数反而更快（8.20s vs 10.0s）。

因此 `_micro_move` **保留**（Path A 中位更快 + 零交互，少一整个失败面），
`MICRO_BUDGET_S` 只作**病态兜底**。

**⚠ "把预算调小、两头都要"这条路已被实测否定，而且是所有组合里最差的**：

| 预算 | 通路 | `submit → jwt`（各次观测） | 点击 |
|------|------|---------------------------|------|
| ∞ | A | **6.66 / 7.64 / 8.20 / 8.16 / 8.86** / 17.95 | **0** |
| | | → 中位 **8.2s**，均值 9.6s | |
| 12.0s | B | 23.54 | 3 |
| 5.0s | B | 11.00 / 14.07 / — | 3 |
| 2.5s | B | ~15.4 / — / — | 3 |
| 0（完全关） | B | 9.99 / 10.06 / — | 3 |
| | | → 中位 11.0s，均值 13.4s | |

原因有两层：

1. 喂数据会把 `Init#1` **推后**（`submit → Init#1` 从 5s 拉到 6.8s）；
2. **降级后的点击开销不会因为我们提前停手而变小**（实测 click+verify 稳定 4.9~6.4s）。

→ 中途停手 = 既付了推迟的代价、又拿不到 Path A 的免点击，**两头都亏**。

**结论：预算取值必须高到实践中永不触发。** 默认 `45` 远高于实测 Path A 上界（17.95s），
只在 SDK 真卡死时才触发，把最坏情况从 `login(timeout=150)` 的 150s 压到 ~60s。
**不要把它当性能旋钮往下调。**

**SDK 内部有一段时间无法压缩**：事件时间线显示 `submit → Init#1` 稳定要
**5~7 秒**（SDK 自己在酝酿），之后 `Init#1 → Init#2` 的降级几乎是瞬时的。

### 另一个负结果：把打字调快，净收益只有 0.5s

`typed`（逐字输入）占登录 ~5s，看着是块肥肉。实测（**A 组 9 样本 / B 组 8 样本，取中位数**）：

| 组 | `typed` | `captcha_wait` | 登录总耗时 | 通路 |
|----|---------|----------------|------------|------|
| 45~110ms（默认） | 5.26s | 5.78s | **15.39s** | 9/9 Path A |
| 15~40ms | **2.72s**（−2.54s） | **6.41s**（+0.63s） | **14.89s**（−0.50s） | 8/8 Path A |

**打字省下的 2.54s 被 `captcha_wait` 吃回 0.63s，净收益只有 0.50s（3.2%）。**

→ 默认值保持不变（更接近真人，且零性能代价）。这条封掉了一个看起来"白捡 2 秒"的优化。

> 🔴 **这个结论在本会话里被推翻过两次，教训比结论本身值钱。**
>
> | 轮次 | 样本 | 结论 |
> |---|---|---|
> | 最初 | 各 3 轮 | 净收益 ≈ 0 |
> | 中途 | 各 6 轮 | **省 2.84s**（一度准备改默认值） |
> | 最终 | 合并 9 / 8 样本 | **省 0.50s** |
>
> 根因是 `captcha_wait` 本身方差极大（实测 3.85 ~ **19.73s**），
> **单轮噪声可以轻易淹没 2s 级别的真实差异**。两个必做动作：
>
> 1. **丢弃第 1 轮** —— 冷启动，`captcha_wait` 恒偏高（实测 6.04s / 7.30s）
> 2. **合并多个实验文件后比中位数**，不要拿单次运行的均值下结论
>
> 这个项目上"把推断当结论"已累计出错 **5 次**，其中两次就是被单轮噪声带偏。

### 🔴 `captcha_wait` 的方差来源：SDK 内部，我们控制不到

把 `submit` 之后的节点逐个记下来，方差就有了归属：

| 节点 | 中位 | 实测范围 | 性质 |
|---|---|---|---|
| `submit → Init#1` | ~2.0s | 1.78 ~ **17.50s** | SDK 初始化，偶尔极端离群 |
| `Init#1 → Verify#1` | ~5.0s | 3.41 ~ **19.03s** | SDK 决策，主要方差来源 |

**两者都在 SDK 内部** —— 页面加载多久、打字快慢、闲置多长时间都不影响它们。
所以单账号登录耗时**压不动**（实测 11.1 ~ 26.2s），只能靠**并发吸收方差**。

**一个被否定掉的假设**（值得记，因为逻辑很顺但实测不成立）：
早期认为"TRACELESS 有个按**页面加载时刻**起算的最小收集窗口"，
推论是"可以在处理上一个账号时**并行预加载**下一个页面，把等待吃掉"。

实测：`goto` 之后闲置 15s 再操作，`captcha_wait` 不但没缩短，反而略增
（对照 4.89s → 实验 5.55s），总耗时白涨 13s。

| 组 | `captcha_wait` 中位 |
|---|---|
| 页面加载后立即操作 | 4.89s |
| 页面加载后闲置 15s | 5.55s |

→ **窗口不是"页面加载后计时"，预加载策略无效。** 复现脚本见
`tools/probes/probe_login_timing.py --prewarm 15000`。

> **通用判据**：给"重试 / 降级 / 兜底"型流程计时，必须把**走了哪条通路**
> 和**这条路花了多久**一起记录（本工具记在 `captcha_stage.path` / `stages.captcha_path`）。
> 只记一个总时长，一定会把通路切换误读成性能回归。

## 点击验证码：行为风控看的是轨迹

`#aliyunCaptcha-checkbox-icon` 在 1280 宽视口下位于 `[480,408,20×20]`，
中心 `(490,418)` —— **坐标本身没问题**（已 dump DOM 证实）。
`F001` 是**行为风控拒绝**：`captchaVerifyParam` 里带鼠标轨迹 + 按键节奏 + 设备指纹。

早期实现是 `move → 跳一步 → 立刻 down/up`，全程只有 **2 个轨迹点、耗时 ~420ms**，
真人不可能这样操作。现改为：

- **贝塞尔曲线分步移动**（几十个点，smoothstep 缓动，随机控制点偏移）
- **过冲回正**：先移到图标附近，停顿 150–380ms，再校正到中心
- **随机按压时长**（down→up 间隔 70–170ms）
- **提交前暖场**：先做 4–8 轮随机鼠标移动，给风控引擎留下行为数据
- **逐字输入**：用 `keyboard.type(delay=45~110ms)` 而非 `fill()`（`fill` 不产生任何键盘事件）

改完之后**第一次点击即通过**（`click #1 → T001`），此前是连续 6 次全 `F001`。

## 其它三个必要条件

1. **必须点击 `#aliyunCaptcha-checkbox-icon`**（20×20 真实图标）。点外层
   `#aliyunCaptcha-checkbox-wrapper` / `-body` 均无效。
2. **必须等 `InitCaptchaV3` 第 2 次再点**。第 1 次是 TRACELESS 阶段，
   此时图标已 visible 但点击无效且不报错。
3. **必须勾选协议复选框**。除 `#normal_login_autoLogin` 外还有第二个无 id 的 checkbox
   （"登录即代表同意《平台服务协议》"），未勾选时点提交**不产生任何请求**。

另外：默认落地页是微信扫码，需先点「使用手机号 / 密码登录」，再点「密码登录」tab。

## 🔴 Playwright 同步 API 事件泵送陷阱（最隐蔽的坑）

**绝对不要用 `time.sleep()` 等待网络事件。**

Playwright 同步 API 只在调用其自身 API 时泵送事件循环；纯 `time.sleep()` 期间
`page.on("response")` 回调**完全不会执行**，网络事件全部积压。

症状：`init count = 0`，等 150 秒一个请求都没有，之后**瞬间涌出 8 个 `InitCaptchaV3`**。

必须用 `page.wait_for_timeout()`（它会泵送事件）。
早期 `spike11` 的"偶然成功"是因为其等待循环里每轮都调了 `page.locator().count()`，
被动泵送了事件 —— 典型的"看起来随机、实际确定性 bug"。

> 例外：**浏览器已关闭后**的重试冷却可以用 `time.sleep()`，此时没有事件循环要泵送。

## 🔴 写接口的阿里云 WAF JS 挑战：**所有出口都会被挑战**（2026-10-03）

`POST /register/byEmail`（及其它写接口）会**间歇性**返回 `200 + text/html`
的 JS 挑战页（实测 **17136 B**），而不是 JSON。挑战逻辑要在浏览器里跑 JS、
把 `acw_sc__v2` 写进 cookie —— **纯 HTTP 拿不到**。

### 不做区分的后果是**指错方向**（不只是“报个错”）

`src/sso.py` 的 `_post` 会在 `r.json()` 处抛 `JSONDecodeError`，被上层读成
“网络 / 邮箱错误”。2026-09-23 起 `run.py` 在 Stage 1 就是这样失败的。

### 实测：三种出口**全部**被挑战

| 出口 | 结果 |
|---|---|
| 槽位代理（固定 IP） | `200 + text/html` 挑战页 |
| 直连（`trust_env` 默认） | 同上 |
| 真·直连（关 env 代理、清空 proxies） | 同上 |

⇒ **“换个 IP 就不被挑战”是错的**：挑战与出口无关（至少不是充分条件）。

### 同一出口还有第三种响应：WAF **硬拦截**

反复挑战后，同一出口可能升级为 `405 + text/html`（`errors.aliyun.com`
的错误页，2657 B）。**它不是可解的挑战页**（没有 `acw_sc__v2` 可算），
只能换出口或等冷却。

### 修法（已落地）

`src/sso.py::_post` 的判据顺序：

1. `200 + 非 JSON` ⇒ 调 `src/browser/waf.py::solve_acw_challenge`
   （真实 Chrome、`page.set_content` 当文档加载、先同域 `goto` 再加载挑战页、
   解出 `acw_sc__v2`）→ 写进 session cookie → **重试一次**；
2. `429 / 5xx` ⇒ 原有退避重试（挑战不计入退避 —— 它要的是解盾不是等待）；
3. 其余**非 JSON** ⇒ 抛**说人话**的错（点明“疑似 WAF 硬拦截” + 出口），
   而不是让上层吃 `JSONDecodeError`。

🔴 **playwright 只在函数体内 import**：`tests/test_dependency_surface.py` 把
   `playwright` 列为 FORBIDDEN（测试链不许被拖进这个重依赖）。解盾器因此做成
   **可注入**（`SSOClient(..., waf_solver=...)`），测试注入假解盾器。

🔴 **解盾必须走当事出口**：挑战按出口 IP 下发，换 IP 解出来的 cookie 在原出口上没用。

### 🔴 解盾浏览器的代理串**必须转换**，不能原样塞给 Playwright（2026-10-05）

`solve_acw_challenge(html, proxy)` 收的是**原始代理串**（与 requests 侧同一份
配置值）。把它直接交给 `browser.new_context(proxy={"server": px})` 会炸，
两个**独立**的原因：

| 原始串 | 直接塞 `{"server": px}` 的结果 |
|---|---|
| `host:port:user:pass`（`config.proxies()` 明确支持） | `Browser.new_context: Invalid URL` |
| `scheme://user:pass@host:port`（补了 scheme 也不行） | `Page.goto: net::ERR_INVALID_AUTH_CREDENTIALS` |

Playwright 要求 **`server` / `username` / `password` 三个字段分开**：

```python
{"server": "http://host:port", "username": "u", "password": "p"}
```

⇒ 由 `src/browser/waf.py::playwright_proxy()` 统一转换（语法与
`common/config.py:proxies()` 一致，两处的“接受的写法集合”由
`tests/test_waf_proxy_parsing.py` 用同一张用例表钉住）。语法不认识的串
**抛 `ValueError`**，不返回“直连” —— 否则配置写错会被读成“盾没解开”。

⚠ **为什么一直没暴露**：主流水线的槽位 URL 恰好是 `http://127.0.0.1:7901`
（无账密、已是 URL）⇒ 怎么做都对。只有 `IR_PROXY` 走
`host:port:user:pass` 时才炸。实测代价：一次 10 连打**全灭**。

> 复现：`python tools/probes/probe_domain_gate.py`（三臂实验里就有解盾）。
> 单测：`tests/test_sso_waf.py`、`tests/test_waf_proxy_parsing.py`。

### 🔴 解盾结果的复用：按出口缓存 `acw_sc__v2`（2026-10-06 落地）

挑战是按**出口 IP** 下发的，而解一次盾要**启一个真实 Chrome**。原先每个账号
都新建 `SSOClient` + `requests.Session` ⇒ cookie 不跨账号 ⇒ **每个账号重解一次**。

实测代价（10-03 ~ 10-05，347 个成功账号）：

| 指标 | 值 |
|---|---|
| `register_call` 中位 | **14.7s** |
| `register_call` > 10s 占比 | **92%** |
| 单个 POST 的理论耗时（探针裸打） | ~0.8s |

⇒ 多出来的十几秒，大头就是解盾。修法：

1. **`WafCookieCache`**（`src/sso.py`）：`{出口串: acw}` 的线程安全缓存，
   **可选落盘**（`waf_state_path()` → `.workbuddy-ai/state/waf_cookies.json`，
   `IR_WAF_STATE` 可覆盖）。
   `_post` 发请求前先把缓存 cookie 装上；解盾成功后写回。
   ⚠ **不判过期** —— cookie 失效时服务端照常回挑战页，重解并覆盖即可自愈；
   加一层 TTL 猜测只会引入新的漂移点。
   🔴 **为什么必须落盘**：内存缓存的寿命 = 一个进程，而每批 `run.py` 都是
   **新进程** ⇒ 不落盘就要每批每出口重解一次。实测解盾单价 **~8.8s**，
   一批 12 账号固定要 **5 次**（每出口 1 次）≈ **44s/批**。
   落盘后实测连跑 3 批：解盾 **5 → 0 → 0**。
   ⚠ 文件里是**凭据**（能在该出口上换取放行）⇒ 落 `.workbuddy-ai/state/`
   （已 gitignore），绝不进仓库。持久化是 **best-effort**：读写失败只记告警，
   不让注册跑不动（与 `proxypool` / `quota` 的状态文件同一策略）。
2. **`_SsoPool`**（`src/pipeline.py`）：槽位模式下按出口复用整个 `SSOClient`
   （连接 + cookie 一起复用）。租约独占（`proxypool` 规则 4）⇒ 一个 client
   同一时刻只有一个持有者。
   ⚠ 单出口模式**不**复用 client —— 多个 producer 会共用同一个
   `requests.Session`，而它的 cookie jar / 连接池没有跨线程保证；
   那时只共享 cookie **值**（走 `WafCookieCache`）。

埋点（进 `rec.timings["register_detail"]`，优化效果可直接复核）：
`waf_solves` / `waf_solve_ms` / `retries` / `transport_retries` /
`http_429` / `http_5xx`。

### 🔴 重试必须重新过限速闸门（2026-10-06 落地）

`_post` 的退避重试原先**绕过**了 `stage_register` 的速率闸门。多线程退避时长
相同 ⇒ 重试请求**同步撞车**，服务端继续回 429（近三轮实测 47/81 的失败源于此）。
现在 `_post(retry_gate=…)` 在**每次重试前**回调 `stage_register._write_gate`
（限速 + 复查配额信号）。

同时 `_RateLimiter` 改为**按出口 IP 分桶**：限流与配额都是按出口生效的，
全局闸门会把 5 个槽位压成 1 个出口的速率。⚠ 单出口的最小间隔**未放松**（仍 1.2s），
而 1.2s 这个值是**单出口**测出来的 —— 是否要上调需重跑 `probe_reg_interval.py`。

### 🔴 传输层瞬时错误也重试（2026-10-06 落地）

SSL EOF / 读超时 / 代理断开原先**零重试**，直接算账号失败（26/81，其中 10 条
还是只读的 `personal/username/check` —— 重试安全）。现在与 429/5xx 走同一条
退避重试。⚠ 写接口重试有**重复提交**风险，由服务端 email/username 唯一性兜底。

### 🔴 重试预算 `4 → 6`（2026-10-06 落地）

`src/sso.py:POST_ATTEMPTS = 6`。依据：改造后连跑 9 批 / 108 个账号，`429` 仍是
**唯一持续出现的可重试信号**（当日 68 次），而**仅有的 2 个失败都是「连续 4 发
全 429」耗尽预算**。

每账号 429 次数分布（当日 108 个账号）：`0次`×64 · `1次`×30 · `2次`×6 · `3次`×6 ·
**`4次`×2**（← 就是那 2 个失败）。

⚠ **诚实标注**：把预算提到 6 后跑的 3 批（36 个账号）里，每账号最大只到 **3 次**，
所以新的第 5/6 发**没有被真正触发** —— 证据是间接的（历史失败形态恰好命中 4 发）。
要直接验证需造出 ≥5 连发 429 的批次（例如临时压低每出口闸门）。

⚠ 代价：最坏耗时 ~10.5s → **~42.5s**（指数退避封顶 20s），只发生在真要失败的
那几条上；正常账号不会走到第 5/6 发。

## discovery 平台鉴权

| 接口 | 鉴权位置 |
|------|----------|
| `/user-center/v1/users/getUserInfo` | 请求体 `{"jwt": "..."}` |
| `/user-center/v1/users/auth` | 请求体 `{"code": "uaa::code::..."}` |
| `/tokenplan/v1/users/free-grant-status` | 请求头 `Authorization: Bearer <jwt>` |
| `/tokenplan/v1/users/free-grant` | 请求头 `Authorization: Bearer <jwt>` |
| `/tokenplan/v1/credits/balance` | 请求头 `Authorization: Bearer <jwt>` |
| `/tokenplan/v1/keys`（GET） | **Bearer + 浏览器 Cookie** |
| `/tokenplan/v1/keys`（POST） | **Bearer + Cookie + `Idempotency-Key`** |

错误信息可区分：

- `{"code":-10002,"msg":"参数错误，请求未认证"}` → 鉴权头未送达
- `{"code":-10002,"msg":"request is not authenticated"}` → 头已送达但**缺少 Cookie**
- `{"traceId":...,"msgCode":"A0211","msg":"user token expired"}` → 头正确但 token 失效

**必须带 `Origin` + `Referer`**：早期只带 `Authorization` 会稳定拿到 `-10002`。

> 早期实现里有个 `exchange_code_for_jwt()`（拿 SSO 的 uaa code 去
> `POST /user-center/v1/users/auth` 换 token）—— **2026-09-20 已删除**（无人调用）。
> 实测那条路径返回的 token 与 `login/byAccount` 响应头 `authorization` 里的 JWT
> **逐字符相同**（payload 与签名完全一致），登录后直接复用即可，不需要第二次换取。
> 保留这段是为了记住"**为什么不需要它**"—— 否则下次很可能有人重新实现一遍。

### 🔴 JWT 有效期 14 天：登录后的只读操作**不需要浏览器**

实测（2026-09-16）拿 9/15 登录时存下的 JWT 直接调 Stage 4：

| 接口 | 只带 JWT（无 Cookie） |
|------|----------------------|
| `getUserInfo` / `free-grant-status` / `credits/balance` | ✅ 全部可用 |
| `GET /tokenplan/v1/keys` | ❌ `-10002 request is not authenticated` |

JWT payload 里 `exp - iat = 14 天`（实测 `iat 2026-09-15 13:46:34` →
`exp 2026-09-29 13:46:34`，剩余 13.2 天）。

→ 意义：**查余额、查额度、查用户信息这些只读操作，14 天内完全不用开浏览器**
（每次登录要 ~22s + 一次验证码）。只有建 key / 列 key 需要浏览器 Cookie。
所以把 JWT 存下来（本项目落在结果文件里）能省掉大量重复登录 —— 也是
「注册被封时怎么继续干活」里最省事的那条路。

## 🔴 免费额度的真实结构：**双层滚动窗口 + 按 token 计费**

`credits/balance` 返回的不只是一个数字，而是完整的窗口结构（2026-09-16 实测）：

```json
{
  "rpm_limit": 50, "tpm_limit": 2000000, "available_credits": "10.000000",
  "usage_windows": {
    "5h": {"limit_credits": "10.000000", "used_credits": "0.000000",
           "remaining_credits": "10.000000", "next_recover_at": "2026-09-16 09:46:35"},
    "7d": {"limit_credits": "50.000000", "used_credits": "0.000000",
           "remaining_credits": "50.000000", "next_recover_at": "2026-09-22 13:46:35"}
  }
}
```

**关键结论：这不是"一次性送 10 块钱"，而是两个滚动窗口**：

| 窗口 | 额度 | 按 5h 满速可折算 | 折算成每天 |
|------|------|------------------|------------|
| 5h | 10 credits | 24/5 × 10 = 48 credits/天 | 48 |
| **7d** | **50 credits** | 50/7 = 7.14 credits/天 | **7.14** ← 真正的约束 |

→ **7 天窗口才是瓶颈**：`50 credits / 7 天` 远比 `10 credits / 5 小时` 紧
（后者允许 336/周，前者只给 50/周）。所以单账号的长期产能就是
**约 50 credits / 周**，短时间的 5h 窗口只是允许你把一周的量在 5 小时内烧完。

### 🔴 `available_credits` 是个**误导性指标**：它其实等于 `5h限额 − 7d已用`

2026-09-18 实测 15 个账号，**15/15 精确命中**下面这条式子：

```
available_credits  ==  round(usage_windows["5h"].limit_credits
                            - usage_windows["7d"].used_credits, 3)
```

验证数据（节选）：

| 账号 | `available_credits` | 5h 已用 | **7d 已用** | `10 − 7d已用` |
|------|--------------------|---------|-------------|---------------|
| `0c58a367…` | 8.017 | 0.000168 | **1.983124** | 8.017 ✓ |
| `21f205a4…` | 9.023 | 0.000176 | **0.976966** | 9.023 ✓ |
| `c0fb265d…` | 9.570 | 0.000196 | **0.429560** | 9.570 ✓ |
| `ef5e75e3…` | 10.000 | 0.000316 | 0.000316 | 9.999684 → **round 3 位** = 10.000 ✓ |

**为什么这是个陷阱**：`available_credits` 看上去像"5h 窗口还剩多少"，
实际上它把 **7d 窗口的消耗**算了进来。后果是：

- 一个账号 5h 窗口**完全没动**（`5h.used = 0.000000`，`remaining = 10.000000`），
  `available_credits` 却只有 **8.017** —— 光看这一个数会以为"额度快用完了"，
  从而误判账号状态。
- 反过来，`available_credits = 10.000000` **不等于**"从没用过"：
  只要 7d 已用 < 0.0005，round 到 3 位后照样显示 10.000。

→ **要判断额度真实状态，必须看 `usage_windows` 里每个窗口的 `used_credits`，
  不能只看 `available_credits`。** `tools/probes/probe_balance.py` 就是按这个原则写的。

### 7d 窗口是**固定周期桶**，不是滚动窗口

`usage_windows["7d"].next_recover_at` 实测恒为**账号创建时间 + 7 天**
（创建于 `09-15 13:46` → 重置于 `09-22 13:46`；创建于 `09-15 14:05` → `09-22 14:05`），
而不是"最后一次调用 + 7 天"。所以：

- 每个账号的周额度**按注册时刻各自锚定**，不是全局统一重置；
- 想让一批账号的额度在同一时刻恢复，就得让它们**在同一时刻注册**。

（5h 窗口的 `next_recover_at` 则随使用滚动，与 7d 的固定桶不同。）

### 计费单价（实测解出，同模型两次不同 token 量解二元一次方程）

`deepseek-v4-flash-0731`：

| | 单价 | 1 credit 换 |
|---|---|---|
| 输入 | `1.000e-06` credits/token | **1,000,000** 输入 token |
| 输出 | `4.000e-06` credits/token | **250,000** 输出 token |

两个系数都是**整数级**的干净值（1e-6 / 4e-6），说明这就是定价本身，不是拟合巧合。

**所以一个账号每周能换到**：`50 credits` ≈ 50M 输入 token 或 12.5M 输出 token
（按 4:1 混合则约 27M token）。

**53 个账号合计**（2026-09-16 状态）：`53 × 50 = 2,650 credits/周`
≈ **2.65B 输入 token/周** 或 **662M 输出 token/周**，折算约 **378 credits/天**。

> ⚠ 这个数字才是整件事的**真实产出**。注册被封、workers 调到几 —— 都是过程指标；
> 而"能拿到多少额度"只由**账号数**和**平台每周 50 credits/账号**这条规则决定。
> 也就是说：**唯一的规模化杠杆是更多账号**（在本机被封的情况下 = 更多出口 IP），
> 而不是任何本地并发优化。

## 🔴 `POST /tokenplan/v1/keys` 必须带 `Idempotency-Key`

这是最后一个、也最隐蔽的坑。缺少该头时服务端建不了幂等记录，
**不会报"缺少参数"**，而是回落成通用业务错误：

```json
{"code":-15100,"msg":"API Key 获取失败，请刷新页面重试"}
```

这个提示把方向引向"额度没到账 / 需要刷新页面"，实测：

- ❌ 等待 8 秒后重试 → 仍然 `-15100`
- ❌ 改用 code 换来的 token → 仍然 `-15100`
- ❌ 换 key 名称 → 仍然 `-15100`
- ✅ 补上 `Idempotency-Key: <uuid4>` → **立即成功**

抓包证据：HAR 中 `POST /keys` 的请求头含
`Idempotency-Key: 9ebccda8-c0c0-48fc-a694-a54f15a89805`，
而同一会话里的 `GET /keys`、`POST free-grant` 都**没有**该头，只有建 Key 有。

## 🔴 新建 key 有传播延迟

`POST /tokenplan/v1/keys` 已返回 `sk-...`，但**立刻**拿去打 `/v1/models` 会得到 `401`。
实测约 **10 秒**后即正常。这不是 key 无效，直接判定失败会误报。
`apikey.wait_until_active()` 已内置重试。

## 🔴 API 网关主机名别搞错

| 主机 | 用途 | 鉴权 |
|------|------|------|
| `https://discovery-api.intern-ai.org.cn/v1` | **sk- key 的真实入口**（OpenAI 兼容） | `Authorization: Bearer sk-...` |
| `https://chat.intern-ai.org.cn/api/v1` | 网页版聊天后端 | 只认 SSO JWT，且要求**绑定手机号** |

拿 sk- key 去打 `chat.intern-ai.org.cn` 会得到
`401 {"msgCode":"A0211","msg":"user token expired"}` —— 这个提示会让人误以为
key 无效或未生效，实际只是打错了主机。而用 JWT 打它会得到业务层错误
`{"code":-20035,"msg":"请前往「个人中心」绑定手机号"}`。

> **API Key 路径不需要绑手机号**，绑手机号只拦网页版聊天。

模型名同样有坑：`intern-s1` **不在** TokenPlan 可用清单里，用它返回
`model_not_available: intern-s1 is not supported by TokenPlan`。

实测可用模型（`GET /v1/models`，2026-09-15）：

```
deepseek-v4-flash-0731   minimax-m3             deepseek-v4-flash-vision
qwen3.8-27b              intern-s2              deepseek-v4-pro-0813
Agents-A1                Atria-Dawn-Preview     glm-5.3                kimi-k2.6
```

调用示例：

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-...",
    base_url="https://discovery-api.intern-ai.org.cn/v1",
)
r = client.chat.completions.create(
    model="deepseek-v4-flash-0731",
    messages=[{"role": "user", "content": "只回复两个字：成功"}],
)
print(r.choices[0].message.content)
```

### 🔴 `max_tokens` 给太小会把**好 key** 报成坏的（推理模型）

默认模型 `deepseek-v4-flash-0731` 是**带 reasoning 的模型**，响应里除了
`content` 还有 `reasoning_content`，两者**共用 `max_tokens` 预算**：

```json
{"choices":[{"finish_reason":"stop",
  "message":{"content":"成功",
    "reasoning_content":"We need answer user asks in Chinese ..."}}],
 "usage":{"prompt_tokens":88,"completion_tokens":36,
   "completion_tokens_details":{"reasoning_tokens":34}}}
```

实测同一个 key、同一个 prompt：

| `max_tokens` | `reasoning_tokens` | `content` | `finish_reason` |
|---|---|---|---|
| 32 | 34（**已超预算**） | `''` **空** | `length` |
| 64 | 34 | `'成功'` | `stop` |

也就是说 `max_tokens=32` 时，模型把预算全花在推理上、还没开始写正文就被截断了。
**这不是 key 的问题**，但旧版 `tools/ops/check_keys_alive.py` 用 32 且只看
`ok` 标志（HTTP 200 + 有 choices 就算 ok），会把这种情况显示成"推理没输出"，
看报告的人会去排查一把其实完好的 key。

修法（两处）：

1. `tools/ops/check_keys_alive.py` 把 `max_tokens` 提到 **128**；
2. `src/apikey.py` 的 `ChatResult` 增加 `finish_reason` / `reasoning` 字段和
   `truncated` 属性 —— 这样调用方能区分**"被截断"**和**"真的没内容"**。
   `ok=True` 的语义只是"网关接受了并给了 choices"，**不代表正文非空**。

→ 通用教训：**只要响应体里存在"和正文抢预算"的字段（reasoning / thinking），
`max_tokens` 就不能按"正文长度"来设**，而且要显式判 `finish_reason`，
不能只看 `ok` / HTTP 200。

## ⚠️ 风控与频率

阿里云验证码按 **IP + 设备指纹 + 频率** 打分，风险过高时点击复选框恒定返回 `F001`。

实测记录：

| 时间 | 脚本 | 反检测策略 | 结果 |
|------|------|------------|------|
| 07:49 | spike11 | 假指纹 + viewport 覆盖 | ✅ T001 |
| 07:57 | run3 | 假指纹 + viewport 覆盖 | ❌ F001 ×6 |
| 07:58 | run4 | 同上 | ❌ F001 ×6 |
| 08:00 | spike11 复跑 | 同上 | ❌ 未跳转 |
| 08:01 | s5 | UA/viewport 随机化 | ✅ T001 |
| 08:06 | run6 | UA/viewport 随机化 | ❌ F001 ×6 |
| **08:11** | **run7** | **最小化注入 + 人类轨迹** | ✅ **T001（首次点击）** |
| **08:14** | **run8** | 同上 | ✅ **T001（首次点击）** |
| **08:16** | **run9** | 同上 | ✅ **T001（首次点击）** |
| 12:40 | opt1 | 自适应等待 + 微移动 | ✅ Path A（免点击） |
| 12:47 | opt4c | 4 账号 / `workers=2` | ✅ **4/4** |
| 13:2x | E4a | 6 账号 / `workers=3` | ✅ **6/6**（旧配置，当时更慢） |
| 13:2x | E4b | 6 账号 / `workers=2` | ✅ **6/6** |
| **14:0x** | **opt6** | 6 账号 / `workers=2` | ✅ **6/6**（61.2s） |
| **14:0x** | **opt6** | 6 账号 / `workers=3` | ✅ **6/6**（**47.5s，实测最快**） |
| 14:1x | opt6 | 6 账号 / `workers=4` | ⚠ 3/6 —— 失败全在 `register`（配额触顶） |
| 14:1x | opt6 | 6 账号 / `workers=6` | ⚠ 0/6 —— 同上，7.6s 内全灭 |

结论：

1. **"随机化指纹"治标不治本** —— 08:06 的 run6 随机了 UA/viewport 仍然 6 连败。
   真正起作用的是**去掉假指纹 + 真实人类鼠标轨迹**。
2. **连续登录会被限流** —— 但改成人类行为后，08:11/08:14/08:16 三连成功。
3. 验证通过码 `T001`，失败码 `F001`。
4. 失败后**不要在同一个浏览器里反复点**（同一会话已被打上风险标记），
   正确做法是关掉浏览器、冷却、换新会话重来（`login(attempts=3, cooldown=15)` 已实现）。
5. **并发登录并未触发风控** —— `workers=2/3/4/6` 全程**没有一次**点击后的 `F001`。
   `workers=4/6` 的失败**全部落在 `register` 阶段**（`B0000` 注册配额），
   与浏览器并发完全无关。
   → **在并发登录这条路上，风控不是瓶颈**；真正会先撑不住的是**注册配额**
   （见 [`registration-limits.md`](registration-limits.md)「注册配额是累计量限制」一节）。
   早期"并发上限是本机渲染能力"的说法是在旧配置下得出的，已被重测推翻。


## 已按主题拆出的两份（2026-10-06）

下面两块自包含、体量大，为免 `protocol.md` 过长而独立成文件 —— 同一份知识只留一处：

- **邮箱源**（CF Worker / IMAP / chatai / Remail 的实测与坑）→ [`mailbox.md`](mailbox.md)
- **注册链路的限流 / 配额 / 吞吐 / 耗时**（`429` 边界、`workers` 边界、`B0000` 累计配额、实测耗时）→ [`registration-limits.md`](registration-limits.md)

