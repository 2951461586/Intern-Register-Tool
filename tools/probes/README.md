# tools/probes/ —— 一次性诊断脚本

**这些不是生产代码，是"改之前先取实测数据"用的。** 跑完就丢，
**结论**进 README 对应章节或 `docs/`。探针本身会被删，结论不会。

写这个 README 的原因：13 个探针 2,100+ 行，如果不写清"每个探针回答什么问题"，
半年后它们就变成考古现场 —— 没人敢删，也没人知道该在什么时候跑。

## 四条规矩

1. **从项目根跑**：`python tools/probes/probe_xxx.py`。
   本目录的 `_path.py` 靠 `sys.path[0]`（= 脚本所在目录）找到 `tools/`，
   再把仓库根加进 `sys.path` —— 换了 cwd 也能跑，但输出里的相对路径会变。
2. **先跑探针，再改代码。** 它的价值是把**推断**变成**实测**。
   `probe_headless.py` 的 docstring 就是范例：「此前'headless 会被识别'是**推断**，
   从未在当前实现下实测过」—— 实测结果是**可用**。
3. **跑完把结论落盘。** 只留在终端里的数字等于没测。
4. ⚠️ 标了「消耗配额」的探针会**真的打注册接口 / 建账号**，会加深 `B0000` 封禁。
   跑之前先确认服务端当前是否已恢复。

## 一览

| 探针 | 回答什么问题 | 什么时候跑 | 看哪个数字 | 结论落在 |
|---|---|---|---|---|
| `probe_429.py` | SSO 网关 429 限流挂在**哪个接口**、按 IP 突发还是并发数、响应里有没有 `Retry-After` | 调整 `workers` 或注册速率前 | 各并发档位的 429 条数、返回延迟（~1.2s 立即返回 = 应用层限流，不是超时） | README「429 限流的真实边界」 |
| `probe_reg_interval.py` ⚠️ 消耗配额 | `register/byEmail` 两次之间的**最小安全间隔**是多少 | 想改 `REG_MIN_INTERVAL`（现值 1.2s）时 | 各间隔档位的 429 数与耗时；间隔直接决定"首账号就绪时刻" | README「429 限流的真实边界」 |
| `probe_quota_scope.py` ⚠️ 消耗配额 | 注册封禁是 **IP 维度**还是**邮箱域名维度** | 怀疑"换发信域名能解封"时 | 同 IP 只换域名后是否**仍**返回 `B0000`（实测：仍封 ⇒ 换域名没用） | README「注册配额是累计量限制」 |
| `probe_register_ip.py` ⚠️ 消耗配额 | 封禁是不是 IP 维度、**换出口 IP 能不能解开** | 接入槽位池后做决定性实验 | 同邮箱域名 / 同参数、只换出口 IP 后的 `msgCode` | README「注册配额是累计量限制」 |
| `probe_balance.py` | 用**已存 JWT** 直查额度（不开浏览器、不登录） | 只想看额度、不想付登录代价时 | `credits/balance` 数值、单账号耗时（~0.5s，可几十路并发） | README「免费额度的真实结构」 |
| `probe_login_only.py` | 只测**登录**阶段（不注册、不建 key）；以及 `workers` 的真实扩展边界 | 注册被封但还想干活时；复测并发度时 | 登录成功率、`--with-discovery` 能否取回 JWT、各 `workers` 档位耗时 | README「workers 的边界」 |
| `probe_login_timing.py` | 登录各段的真实耗时拆解 | 优化登录耗时前（先归因，别猜） | `goto / form_ready / typed / checkbox / warmup / captcha_ready` 六段；`captcha_wait` 恒 ~4.3–4.5s | README「实测耗时」 |
| `probe_login_route.py` | SSO 登录页有没有"**直达密码表单**"的路由 | 想省掉进表单的两次点击时 | `form_ready` 现状 **3.3s**；若 SPA 会把路由同步到地址栏，就能直接 `goto` | README「实测耗时」 |
| `probe_captcha_timing.py` | 验证码初始化等待期做鼠标微移动，**加快还是拖慢** `TRACELESS→CHECK_BOX` 降级 | 怀疑"暖场反而变慢"时 | 事件时间线（粗粒度总耗时无法归因，必须拿时间线） | README「验证码有两条通路」 |
| `probe_headless.py` | **无头**浏览器能不能过验证码（H1/H2/H3 三组配置） | 想省掉有头窗口时 | 每组是否通过 + dump 出的指纹里哪几个字段异常 | README「无头浏览器：实测可用」 |
| `probe_env.py` | `viewport=` 覆盖 vs `--window-size` 两种模式的**指纹一致性** | 改浏览器启动参数时 | 是否出现 `innerWidth > outerWidth` —— 真实浏览器不可能出现的硬性自动化特征 | README「浏览器登录：反检测要少做」 |
| `probe_proxy.py` | 代理**真的能用于本项目**吗（换出口绕开注册封禁） | 换代理 / 换订阅时 | **出口 IP**。🔴 不看 TCP 连通性（TUN + fake-ip 会给你 0.02s 的假连通），也不看状态码，要看响应正文 | README「其它三个必要条件」 |
| `probe_slots.py` | **有多少个槽位 ≠ 有多少个出口 IP**，以及每个出口能不能到目标站 | 生成 `slots.txt` 后、跑批量前 | `SLOT-nn (端口) -> 出口 IP` 映射表；映射错了配额会被记到别的 IP 头上 | `tools/ops/gen_mihomo_slots.py` 的 docstring |

## 已经不再需要跑的

这几条结论已经被**生产代码**吸收了，探针只作为取证记录保留：

| 探针 | 为什么不用再跑 |
|---|---|
| `probe_429.py` / `probe_reg_interval.py` | `pipeline.REG_MIN_INTERVAL` + `_RateLimiter` 已按实测值钉死 |
| `probe_quota_scope.py` | 结论已写进 `src/quota.py` 的模块 docstring（IP 维度、窗口 > 8.6h） |
| `probe_slots.py` | 出口映射由 `config.SLOT_EGRESS_IPS` + `slot_scope()` 承担，错配会当场抛错 |
| `probe_headless.py` | 结论已落地：`BrowserSession(headless=...)` 支持无头 |

## 加新探针时

- 命名 `probe_<回答的问题>.py`，**不要**用 `test_` 开头（那会让 pytest 误收）。
- 文件头写清三段：**背景**（为什么怀疑）、**做法**（怎么控制变量）、
  **判据**（看到什么数算结论成立）。`probe_proxy.py` 的三条"坑"写法可以照抄。
- 只要产出"改之前 / 改之后"的对比数据，就值得单独一个探针 ——
  别把探针逻辑塞进 `src/`，那会污染生产代码的 import 路径。
