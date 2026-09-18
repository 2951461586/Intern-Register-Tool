# Intern-Register-Tool

OpenXLab（上海人工智能实验室）账号自动注册 + API Key 提取工具。

结合 CF Worker 域名邮箱（已适配激活链接自动提取）与真实浏览器，跑通
**注册 → 邮件激活 → 登录取 JWT → 领取免费额度 → 创建 API Key → 验证可调用** 全链路，
并做成**两段式并发流水线**，可直接批量出号。

实测：单账号 **~35s**（顺序）；批量 **11~14s / 账号**（`--workers 2`）。

## 快速开始

```bash
# 1. 依赖
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt

# 2. 配置凭据（必做 —— 代码里不写死任何 token）
cp .env.example .env
#   然后编辑 .env，填入 IR_WORKER_ADMIN_TOKEN

# 3. 跑一个账号
python run.py

# 4. 批量 6 个（默认 2 路浏览器并发，注册阶段自动流水线重叠）
python run.py --count 6 --workers 2 --out keys.json

# 5. 无头模式（不弹窗口，实测可用）
python run.py --count 6 --headless

# 6. 保存过程截图（排查用）
python run.py --shot debug
```

> 🔴 **凭据一律走 `.env` 或环境变量**，代码里没有任何硬编码 token。
> 缺少必填项时 `run.py` 会在启动阶段直接报错退出（退出码 1），
> 而不是跑到一半才发现全是 401。`.env` 已在 `.gitignore` 中，不会进仓库。
> 环境变量优先级高于 `.env`，临时覆盖可直接 `IR_XXX=1 python run.py`。

本机 Chrome 路径默认 `C:\Program Files\Google\Chrome\Application\chrome.exe`，
可通过环境变量 `IR_CHROME_PATH` 覆盖。

### 环境变量

**凭据（必填，二选一）**

| 变量 | 默认 | 说明 |
|------|------|------|
| `IR_WORKER_ADMIN_TOKEN` | **无 —— 必填** | CF Worker 的 Admin Token。缺失时启动阶段即报错退出 |
| `IR_YYDS_API_KEY` | **无** | YYDS Mail 的 API Key（`IR_MAIL_PROVIDER=yyds` 时必填） |

**临时邮箱提供者**

| 变量 | 默认 | 说明 |
|------|------|------|
| `IR_MAIL_PROVIDER` | `worker` | `worker` = CF Worker 临时邮箱；`yyds` = YYDS Mail 临时邮箱 |
| `IR_YYDS_BASE_URL` | `https://maliapi.215.im/v1` | YYDS Mail API 地址 |
| `IR_YYDS_DOMAIN` | 空（API 默认） | 建邮箱使用的域名（可选） |
| `IR_YYDS_SUBDOMAIN` | 空 | 建邮箱使用的子域名（可选） |

**可选（都有实测默认值，通常不用动）**

| 变量 | 默认 | 说明 |
|------|------|------|
| `IR_WORKER_BASE` | 远端 Worker 地址 | 临时邮箱服务地址 |
| `IR_WORKER_DOMAIN` | `liziai.cloud` | 建邮箱使用的域名 |
| `IR_CHROME_PATH` | 本机 Chrome | 浏览器可执行文件 |
| `IR_CHAT_API_BASE` | `https://discovery-api.intern-ai.org.cn/v1` | 推理网关 |
| `IR_MICRO_BUDGET` | `45` | 鼠标喂数据预算（秒）。**只能往大调**，往小调会稳定拿到更差的 Path B，见下 |
| `IR_TYPE_DELAY_LO` / `_HI` | `45` / `110` | 逐字输入的按键间隔（毫秒）。**已实测调小净收益仅 0.5s**，见下 |
| `IR_NO_MICRO_MOVE` | 未设 | 置 `1` 关闭鼠标微移动（**仅用于对照实验**，生产不要开） |
| `IR_PREWARM_MS` | `0` | `goto` 后闲置 N 毫秒再操作（**仅用于对照实验**，见「方差来源」） |


## 架构

| 阶段 | 方式 | 说明 |
|------|------|------|
| 1. 注册 | 纯 HTTP | `POST /register/byEmail`，**无需人机验证** |
| 2. 激活 | 纯 HTTP | Worker 收信 → `extracted_json` 直接给出激活链接 |
| 3. 登录 | **浏览器** | 阿里云验证码 2.0，纯 HTTP 无法通过 |
| 4. 建 Key | 纯 HTTP | discovery tokenplan 接口（**需 `Idempotency-Key`**） |
| 5. 验证 | 纯 HTTP | `GET /v1/models` 确认 key 可调用（非致命） |

### 项目结构

```
run.py                 CLI 入口（含启动配置校验）
requirements.txt       运行依赖
.env.example           凭据模板（复制为 .env 后填值）
.gitignore             排除 .env / 运行产物 / .workbuddy-ai

src/
  config.py            配置与常量（.env 加载、启动校验、模型清单）
  crypto_rsa.py        RSA 密码加密（复刻前端逻辑）
  tempmail.py          CF Worker 临时邮箱客户端（自适应轮询窗口）
  base.py              临时邮箱提供者抽象基类（MailProvider）
  yydsmail.py          YYDS Mail 临时邮箱提供者（验证码 / 激活链接提取）
  yyds_client.py       YYDS Mail 适配器（兼容 TempMailClient 接口）
  sso.py               SSO 注册 / 激活
  browser_login.py     浏览器登录 + 验证码处理 + JWT 提取
  discovery.py         discovery 平台（额度 / API Key）
  apikey.py            推理网关客户端（OpenAI 兼容）
  pipeline.py          端到端编排（两段式流水线）

tools/                 诊断探针 —— 不参与主流程，用来给"改之前 / 改之后"取实测数据
  probe_429.py             注册限流边界探测（绕开退避重试打裸请求）
  probe_reg_interval.py    注册闸门间隔降序试探（见 429 边界表）
  probe_captcha_timing.py  验证码通路 / 微移动预算对照实验（见「两条通路」）
  probe_login_timing.py    登录时序探针：打字间隔 / 页面闲置对照实验（带事件时间线）
  probe_login_route.py     登录页是否存在"直达密码表单"路由
  probe_headless.py        无头模式可用性验证
  probe_env.py             浏览器环境指纹导出
```

> `tools/` 下的探针一律**从项目根**执行，例如 `python tools/probe_429.py`
> （脚本内部按 `parents[1]` 定位项目根，不要 `cd tools` 后再跑）。

---

## 并发编排：两段式流水线

### 为什么不是"N 个线程各跑全链"

三个阶段资源特性完全不同，用同一种并发度绑在一起会浪费稀缺资源：

| 阶段 | 资源 | 特性 | 可并发度 |
|------|------|------|----------|
| 注册 + 邮件激活 | 纯 HTTP | 快（~10s） | 高（4~8 路无压力） |
| 登录过验证码 | **浏览器** | 慢（~20s） | **受本机渲染能力限制** |
| 建 Key + 校验 | 纯 HTTP | 快（~1s） | 高 |

如果每个线程跑完整链路，**浏览器并发度会被注册阶段的并发度牵着走**，
而浏览器恰恰是最贵的那一环。

### 结构

```
┌─ 生产者池（并发 4）────────┐      ┌─ 消费者池（并发 = workers）──┐
│ 建邮箱 → 注册 → 收信 → 激活 │ ───▶ │ 登录 → 领额度 → 建 Key        │
└──────────────────────────┘ 队列  └─────────────────────────────┘
```

注册（~10s）被隐藏进登录（~20s）里，再叠加 `workers` 路并行。

### 🔴 429 限流的真实边界：挂在**写操作**上，不在并发度上

| 接口 | 类型 | 8 路并发 | 4 路并发 |
|------|------|----------|----------|
| `personal/username/check` | 只读 | ✅ 零限流 | ✅ |
| `register/byEmail` | **写** | — | ❌ **3 路立刻 429**（~1.2s 返回，不是超时） |

所以限流挂在**写操作 + 突发**上，不是笼统的 IP 速率限制。
正确手段是**速率闸门**（`_RateLimiter`），不是降低并发度：

```python
REG_MIN_INTERVAL = 1.2   # 两次 register/byEmail 之间的最小间隔
```

**边界是实测出来的**（`.workbuddy-ai/tmp/probe_reg_interval.py`）——
关键是要**绕开 `_post()` 的退避重试**打裸请求，否则 429 被吞掉，永远探不到边界：

| 间隔 | 成功 | 429 | 平均耗时 |
|------|------|-----|----------|
| 2.5s | 4 | 0 | 0.89s ✅ |
| 2.0s | 4 | 0 | 0.81s ✅ |
| 1.5s | 4 | 0 | 0.80s ✅ |
| 1.0s | 4 | 0 | 0.80s ✅ |

四档全清 —— 原先拍脑袋定的 **2.5s 保守了 2.5 倍**。取实测干净的 1.0s + 20% 余量 = 1.2s。

> ⚠ **但收窄它并不带来吞吐提升**：`workers=2` 时浏览器侧消耗速率是
> `2/20s ≈ 0.10 账号/秒`，而 1.2s 闸门给出 `0.83 账号/秒` —— 快 8 倍。
> **注册根本不是瓶颈。** 收窄闸门的真实收益只有：减少 worker 冷启动空转、
> 避免队列堆深。

### 🔴 `workers` 的边界：风控放行到 3，但**注册配额**先撑不住

实测 6 账号，`--headless`，同一次会话内连续跑：

| 配置 | 成功 | 关键路径 | 每账号 | 登录耗时分布（秒） |
|------|------|----------|--------|--------------------|
| `--workers 2` | 6/6 | 61.2s | 10.2s | 12.5 · 13.7 · 15.6 · 15.8 · 16.1 · 17.0 |
| `--workers 3` | 6/6 | **47.5s** | **7.9s** | 14.2 · 16.1 · 16.7 · 17.0 · 18.4 · 21.3 |
| `--workers 4` | **3/6** | — | — | 失败全在 **register** 阶段 |
| `--workers 6` | **0/6** | — | — | 同上，7.6s 内全灭 |

**`workers=3` 比 `workers=2` 快 23%**，且 6/6 全成功、零 `F001`。

> ⚠ 这一条**推翻了本项目早期的结论**。早期测出"workers=3 更慢"
> （`form_ready` 1.78s → 11.53s），那是**旧配置 + 本机负载不同**时的数据。
> 换配置后重测，方向完全相反 —— **"以前测过"不等于"现在仍然成立"**。

**`workers=4` 的失败与 workers 无关**：`reg_conc = min(count, REG_CONCURRENCY)`
恒为 4，改 `workers` 根本不影响注册并发度。真实原因是**注册配额触顶**，见下节。

默认仍保守取 `2`（`workers=3` 只测过一次，且当时已接近配额边界，需复测）。
换更强的机器值得重测 3~4。

### 🔴 注册配额是**累计量**限制，不是瞬时速率

`REG_MIN_INTERVAL` 只管住了**瞬时速率**（两次 `register/byEmail` 之间至少 1.2s）。
平台还有一层**累计配额**：

| 时段内累计注册 | 现象 |
|---|---|
| 0 ~ 约 40 个 | 全部成功 |
| 约 40 个之后 | 开始零星 `B0000 请求频繁，请稍后再试` |
| 再往后 | 连**单账号**都注册不了，等待数分钟仍未恢复 |

实测触发过程（同一次会话连续跑）：

```
opt6_baseline  尝试 6  成功 6   B0000 0
opt6_w3        尝试 6  成功 6   B0000 0
opt6_w4        尝试 6  成功 3   B0000 3   ← 开始触顶
opt6_w6        尝试 6  成功 0   B0000 6
probe_quota    尝试 1  成功 0   B0000 1   ← 单账号也失败，限流未恢复
```

**三个判据，缺一不可**：

1. 失败**全部落在 `register` 阶段**（不是登录）→ 不是浏览器 / 行为风控问题
2. 改 `workers` **无效** → 它不控制注册并发度
3. 调 `REG_MIN_INTERVAL` **也无效** → 那是瞬时闸门，管不到累计量

`run.py` 现在会在报告里识别并明确提示这个模式，避免往错的方向排查。

### 🔴 把"有传播延迟的校验"挪到流水线末尾

新建 API Key 在网关侧有 **~10s 传播延迟**。若每个账号建完就地等它生效，
等于给每个账号白加 ~7s。

挪到流水线末尾统一做（`verify_keys()`）时，第一个账号的 Key 早就过了传播期，
校验几乎瞬时：

| | 建 Key 阶段耗时 |
|---|---|
| 关键路径内就地校验 | 7.3 ~ 8.6s |
| **末尾统一校验** | **0.7 ~ 1.0s** |

> **通用判据**：关键路径上任何"等待外部系统同步"的步骤，先问一句
> **"能不能挪到最后一起等？"** —— 最后一个账号需要的等待时间不变，
> 前面所有账号的等待可以完全隐藏。


## 协议要点

### 密码加密（最容易踩的坑）

前端 `main.59963db7.chunk.js` 的真实实现：

```javascript
a.setPublicKey(pubKey);
a.encrypt(email + "||" + password + Math.floor(Date.now() / 1e3))
```

即明文为 `f"{identity}||{password}{unix_seconds}"`，再做 **RSA/ECB/PKCS1Padding**，最后 base64。

- ❌ 只加密 `password` → 服务端报 `A0216 用户密码解密失败`
- ✅ 带 `identity||` 前缀与秒级时间戳 → 成功
- 三种场景的 identity：注册用 `email`，登录用 `account`，改密用 `email`

### 人机验证（核心难点）

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

### 浏览器登录：反检测要"少做"而不是"多做"

🔴 **这是本项目最反直觉的一条。**

本机真实 Chrome 为 `152.0.7977.83`。实测（`.workbuddy-ai/tmp/probe_env.py`）：
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

### 🔴 验证码有两条通路：`captcha_wait` 不是一个能直接比较的数

**这是本项目最容易误判的一条。** 早期我把"优化前 4s / 优化后 8.19s"当成性能回归，
其实是**拿两条不同的通路在比**。

对照实验（`.workbuddy-ai/tmp/probe_captcha_timing.py`，各 3 轮，6/6 成功）：

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

#### 另一个负结果：把打字调快，净收益只有 0.5s

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

#### 🔴 `captcha_wait` 的方差来源：SDK 内部，我们控制不到

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
`tools/probe_login_timing.py --prewarm 15000`。

> **通用判据**：给"重试 / 降级 / 兜底"型流程计时，必须把**走了哪条通路**
> 和**这条路花了多久**一起记录（本工具记在 `captcha_stage.path` / `stages.captcha_path`）。
> 只记一个总时长，一定会把通路切换误读成性能回归。

### 点击验证码：行为风控看的是轨迹

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

### 其它三个必要条件

1. **必须点击 `#aliyunCaptcha-checkbox-icon`**（20×20 真实图标）。点外层
   `#aliyunCaptcha-checkbox-wrapper` / `-body` 均无效。
2. **必须等 `InitCaptchaV3` 第 2 次再点**。第 1 次是 TRACELESS 阶段，
   此时图标已 visible 但点击无效且不报错。
3. **必须勾选协议复选框**。除 `#normal_login_autoLogin` 外还有第二个无 id 的 checkbox
   （"登录即代表同意《平台服务协议》"），未勾选时点提交**不产生任何请求**。

另外：默认落地页是微信扫码，需先点「使用手机号 / 密码登录」，再点「密码登录」tab。

### 🔴 Playwright 同步 API 事件泵送陷阱（最隐蔽的坑）

**绝对不要用 `time.sleep()` 等待网络事件。**

Playwright 同步 API 只在调用其自身 API 时泵送事件循环；纯 `time.sleep()` 期间
`page.on("response")` 回调**完全不会执行**，网络事件全部积压。

症状：`init count = 0`，等 150 秒一个请求都没有，之后**瞬间涌出 8 个 `InitCaptchaV3`**。

必须用 `page.wait_for_timeout()`（它会泵送事件）。
早期 `spike11` 的"偶然成功"是因为其等待循环里每轮都调了 `page.locator().count()`，
被动泵送了事件 —— 典型的"看起来随机、实际确定性 bug"。

> 例外：**浏览器已关闭后**的重试冷却可以用 `time.sleep()`，此时没有事件循环要泵送。

### discovery 平台鉴权

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

> `exchange_code_for_jwt` 是多余的：实测 `POST /user-center/v1/users/auth` 返回的 token
> 与 `login/byAccount` 响应头 `authorization` 里的 JWT **逐字符相同**（payload 与签名完全一致），
> 登录后直接复用即可。

### 🔴 `POST /tokenplan/v1/keys` 必须带 `Idempotency-Key`

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

### 🔴 新建 key 有传播延迟

`POST /tokenplan/v1/keys` 已返回 `sk-...`，但**立刻**拿去打 `/v1/models` 会得到 `401`。
实测约 **10 秒**后即正常。这不是 key 无效，直接判定失败会误报。
`apikey.wait_until_active()` 已内置重试。

### 🔴 API 网关主机名别搞错

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

### ⚠️ 风控与频率

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
   （见「注册配额是累计量限制」）。
   早期"并发上限是本机渲染能力"的说法是在旧配置下得出的，已被重测推翻。

### CF Worker 临时邮箱

```
POST /api/mailboxes        {"domain": "liziai.cloud", "count": N} -> {"emails": [...]}
GET  /admin/all?limit=N    邮件列表（含 extracted_json 已提取的链接）
GET  /admin/msg?id=&email= 单封详情
GET  /health               {"ok":..., "database":..., "domains":..., "storage":...}
```

鉴权头 `X-Admin-Token` 与 `Authorization: Bearer` 都支持，两个都带最稳。
可用域名：`liziai.cloud` / `edu.liziai.cloud` / `liziai.kdns.fr` / `lizi.kdns.fr` /
`tilian.eu.cc` / `liziapi.eu.cc` / `xmlizi.eu.cc`。

`received_at` 是**毫秒** unix 时间戳（形如 `1789449135216`），不是秒。

#### 🔴 `/admin/all` 的两个实测特性（决定了轮询怎么写）

**① 不支持按收件人过滤。** `email` / `to` / `to_address` 三个参数**全被忽略** ——
传了和不传返回体**逐字节相同**（都是 57289 字节、50 条）。所以只能整表拉回来自己筛。

**② 延迟与返回体积正相关**（这一条更正了早先"延迟与 limit 无关"的错误结论）：

| `limit` | 耗时 | 体积 |
|---------|------|------|
| 50 | **568ms** | 57 KB |
| 5 | **265ms** | 5.8 KB |
| 1 | 269ms | 1.2 KB |

50→5 直接减半，只有 ~265ms 是真正的往返底座。
固定 `limit=50` 意味着**每次轮询拉 57KB**；4 个生产者并发轮询就是 ~170KB/s
砸向 Worker，既白等 300ms 又挤带宽。

→ 所以轮询用**自适应窗口**（`tempmail.wait_for_mail`）：
从 `MAIL_LIST_MIN=5` 起步，**未命中就翻倍**，上限 `MAIL_LIST_LIMIT=50`。
注册期绝大多数轮询会立刻命中（走小包），真碰上 Worker 繁忙再自动放大，不会漏。

**效果实测**：轮询开销 **1.79s → 0.41s（降 77%）**。
但邮件到达本身要 ~5.9s，所以注册阶段总时长基本没动 ——
**这笔优化的价值在"少砸 10 倍带宽给共享 Worker"，不在省时间。**

#### 🔴 注册阶段的真实瓶颈：收信轮询，不是注册接口

子阶段计时（`rec.timings["register_detail"]`）把注册拆开：

| 子阶段 | 耗时 | 说明 |
|--------|------|------|
| `mailbox` | 0.66s | 建邮箱 |
| `username` | 0.63s | 生成 + 查重 |
| `gate_wait` | 2.2~3.4s | 限速闸门（并行，不占关键路径） |
| `register_call` | **0.80s** | 注册接口本身很快 |
| `mail_wait` | **7.8s** | 🔴 **占总注册时间 61%** |
| `activate_call` | 0.04s | 激活接口 |

`mail_wait` 又被拆成两段（`arrival_delay_ms` / `poll_overhead_ms`）：

```
mail_wait 7.81s  =  邮件真正到达 6.02s  +  我们轮询的钝度 1.79s
                     ↑ 无解（等 SMTP + Worker 入库）   ↑ 调 limit/interval 就行
```

**这 6s 是硬下限** —— 邮件从发出到出现在 Worker 列表里就要这么久。
加上其余环节，单账号注册最快 ~8.3s，**没有进一步压缩空间**。

> **方法论**：一个 7.9s 的黑盒阶段，拆开是两个性质完全不同的部分 ——
> 不拆就只能猜，而这两者的修法**方向相反**（一个该放弃，一个该优化）。
> 任何超过总耗时 20% 的阶段，都要拆成"上游固有延迟 + 我方可控开销"再决定动不动手。



## 实测耗时

### 单账号（顺序，`--workers 1`）

| 阶段 | 耗时 |
|------|------|
| 注册 | ~0.8 s |
| 激活（含收信） | ~6–9 s |
| 浏览器登录 | ~18–27 s |
| 领额度 + 建 Key | ~0.7 s |
| key 生效校验（末尾统一） | ~0.3 s |
| **合计** | **~35 s / 账号** |

登录内部阶段（典型值）：

```
goto             +  1.4 ~  2.5s
form_ready       +  1.8 ~ 11.5s   ← 本机渲染争用时主要膨胀在这一项
typed            +  4.9s          ← 逐字输入，延迟是行为信号，不要调小
checkbox         +  0.04s
warmup           +  1.3 ~  1.5s
captcha_ready    +  6.5 ~ 18.0s   ← 看走 Path A 还是 Path B
done             +  0.00s
```

### 批量吞吐

| 配置 | 账号数 | 关键路径 | 每账号 | 备注 |
|------|--------|----------|--------|------|
| 顺序（优化前） | 1 | 53.7s | 53.7s | |
| 顺序 + 自适应等待（优化后） | 1 | 35.5s | 35.5s | |
| `--workers 2` headless | 3 | 51.9s | 17.6s | 末轮空转 |
| `--workers 2` headful | 4 | 44.8s | **11.2s** | |
| `--workers 2` headless | 6 | 79.2s | 13.2s | ⚠ 旧配置 |
| `--workers 3` headless | 6 | 88.2s | 14.7s | ⚠ 旧配置（当时更慢） |
| `--workers 2` headless | 6 | 61.2s | 10.2s | 当前配置 |
| **`--workers 3` headless** | **6** | **47.5s** | **7.9s** | **当前配置，实测最快** |

优化链路：**53.7s/账号（顺序）→ 35.5s（自适应等待）→ 7.9 ~ 10.2s（并发流水线）**。

> 🔴 **标"旧配置"的两行结论已失效**：当时测出 `workers=3` 更慢，
> 换配置后重测方向完全相反（见「workers 的边界」）。
> 保留这两行是为了说明一件事 —— **性能数据必须带配置版本**，
> 否则过一阵你会拿一个已经失效的数字当依据。
>
> 同理，"单账号登录耗时"这类数字的方差本身就有 11.1 ~ 26.2s，
> **单次运行的均值不具备可比性**，要看中位数 + 样本数。

> **⚠ 账号数最好是 `workers` 的整数倍。**
> 关键路径 = `首账号就绪 + ceil(count / workers) × 单账号登录`。
> `count=3, workers=2` 时要跑 2 轮、第 2 轮只有 1 个账号（另一个 worker 空转），
> 每账号摊到 17.6s；`count=4` 时两轮都满，降到 11.2s。
> 所以**批量跑就凑整**，别跑 3 个、5 个这种数。



## 输出

`results.json` 每个账号一条记录：

```json
{
  "email": "oai-xxxxxxxx@liziai.cloud",
  "username": "lz123456",
  "password": "Lz#xxxxxxxxx",
  "sso_uid": "415100668",
  "jwt": "eyJ0eXBlIjoiSldUIi...",
  "api_key": "sk-...",
  "key_id": "ak_...",
  "credits": "10.000000",
  "status": "success",
  "stages": {
    "register": "ok",
    "activate": "ok",
    "login": "ok",
    "key": "ok",
    "verify": "ok(10 models)"
  }
}
```

## 能不能走纯协议？——不能，成本极高

完整拆过验证码链路（HAR entry #1 ~ #12），结论是**协议复刻理论上可行，但工程上不划算**。

链路本身是标准阿里云 OpenAPI 签名（`HMAC-SHA1` + `SignatureNonce` + `Timestamp`），
这部分可以复刻。真正的拦路虎是三个**不可控字段**：

| 字段 | 出处 | 体积 | 性质 |
|------|------|------|------|
| `DeviceData` | `InitCaptchaV3` 请求 | 192 B | 设备指纹摘要 |
| `Data` | `Log2` / `Log3` 请求 | **5440 / 5528 B** | 设备指纹全量采集 |
| `DeviceToken` | `InitCaptchaV3` 响应 | 1276 B | **服务端签名** |

`DeviceToken` 解码后结构是：

```
WEB#<machineId>-h-<毫秒时间戳>-<随机数>#<签名>
```

末段签名由阿里云服务端签发，**无法自行伪造** —— 而它正是登录时
`captchaVerifyParam` 的必需组成。

那 5KB 设备数据由 `feilin017.js` + `cx.063.js` 生成。关键在于
`cx.063.js` 是**动态下发的混淆 JS**（`InitCaptchaV3` 响应里的
`StaticPath: 3.29.0/cx.063.9ddae7d638b970c0`），版本与 hash 会变。

所以走纯协议要持续对抗一个**会变的混淆 SDK** + 复刻 5KB 指纹算法 +
拿到服务端签名。任何一次 SDK 升级就全部失效。**不值得。**

## 无头浏览器：实测可用 ✅

> ⚠️ 早期文档写过"`--headless` 会被验证码识别，必须 headful" —— **那是推断，且是错的。**
> 在最小化注入 + 人类轨迹的实现下实测三组无头配置（`.workbuddy-ai/tmp/probe_headless.py`）：

| 配置 | 额外参数 | 结果 | 耗时 |
|------|----------|------|------|
| H1 基础无头 | 无 | ✅ `click #1 → T001` | 47s |
| H2 显式窗口尺寸 | `--window-size=1280,720 --force-device-scale-factor=1` | ✅ `click #1 → T001` | 37s |
| H3 软件 GPU | 再加 `--use-gl=angle --use-angle=swiftshader` | ✅ `click #1 → T001` | 31s |

**三组全部首次点击即通过**，且端到端 `python run.py --headless` 也跑通（`status=success`）。

两个反直觉之处：

1. 无头模式下 UA 里**明写着 `HeadlessChrome/152.0.0.0`**，照样通过；
2. 无头模式下 `outerWidth == innerWidth == screen.width`（没有窗口边框），
   GPU renderer 也如实上报（H1/H2 是真实 NVIDIA RTX 3050，H3 是 SwiftShader），同样通过。

**结论：阿里云这套风控主要看行为轨迹，不是 UA / 窗口 / GPU 指纹。**
这也解释了为什么早期"随机化 UA + viewport"完全无效、而"人类鼠标轨迹"一次就过。

实现上只做一处覆盖：无头模式把 UA 里的 `HeadlessChrome` 归一化为 `Chrome`
（**版本号原样保留**，取 `browser.version` 首段拼 `Chrome/152.0.0.0`）。
这不是伪造指纹，而是去掉一个自动化工具留下的无意义自我标记。

```bash
python run.py --headless        # 无头跑，不弹窗口
```

## 已知限制

- 验证码若切到滑块模式（`captchaType` 非 `CHECK_BOX`）需另加滑动轨迹模拟，
  当前实现只检测并上报（`captcha_stage.slider`），不做自动破解
- **`workers` 的上限由本机渲染能力决定，不由风控决定** —— 实测 `workers=3`
  风控全放行（6/6）但吞吐反而更差（`form_ready` 1.78s → 11.53s）。
  换机器需要重测
- 登录页**没有"直达密码表单"的路由**（SPA 用内部状态切 tab，URL 不变，已实测）。
  `form_ready` 的水合时间无法通过改 URL 省掉
- 免费额度为 `pkg_free_monthly_base`，10 credits / 5 小时窗口，rpm 50
- 单账号登录仍需 ~15–27s，其中 `submit → Init#1` 的 5~7s 是验证码 SDK
  内部酝酿时间，**无法压缩**
- **注册阶段已到硬下限**：激活邮件真正到达就要 **6.02s**（SMTP + Worker 入库，
  实测拆解见「CF Worker 临时邮箱」），加上建邮箱/查重/注册/激活，
  单账号注册最快 ~8.3s，没有进一步压缩空间
- **登录耗时方差大**（实测极差可达 18.7s）。批量总时长由**最慢那个账号**决定，
  不是均值 —— 所以报告里打印的是最慢账号的分解，不是第一个

---

## 免责声明

本项目是**协议逆向与浏览器自动化的技术研究**，目的在于搞清 HTTP 链路与前端风控
（阿里云验证码 2.0）的交互机制。

- 请勿用于批量刷取、账号转售或任何违反目标平台服务条款的用途
- 工具**不绕过任何付费环节**，也不篡改额度 —— 领的就是平台公开提供的免费额度
- 使用产生的任何后果由使用者自行承担

仓库内**不含任何账号数据**。`results.json`（含明文账号/密码/JWT/API Key）、
`.env`（含 Admin Token）、`.workbuddy-ai/`（本地开发记录）均已在 `.gitignore` 中排除。

## 许可

MIT
