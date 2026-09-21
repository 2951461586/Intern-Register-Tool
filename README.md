# Intern-Register-Tool

OpenXLab（上海人工智能实验室）账号自动注册 + API Key 提取工具。

结合 CF Worker 域名邮箱（已适配激活链接自动提取）与真实浏览器，跑通
**注册 → 邮件激活 → 登录取 JWT → 领取免费额度 → 创建 API Key → 验证可调用** 全链路，
并做成**两段式并发流水线**，可直接批量出号。

实测：单账号 **~35s**（顺序）；批量 **10.2s / 账号**（`--workers 2`）、
**2.4s / 账号**（`--workers 12`，只测登录阶段，见「workers 的边界」）。

> **本仓库是公开的。** 凭据只进 `.env`（代码里一律 `os.getenv()` 且默认空，
> 缺项由 `config.validate()` 在入口报错）；风控标识（出口 IP / Worker 子域 /
> 邮箱域名 / 代理账密 / 本机绝对路径 / 订阅名）**一律按家族占位化**，
> 出口 IP 用 RFC 5737 保留段 `203.0.113.x`。
>
> 完整规范（标识分类、目录规范、闸门两层关系、事故处置与轮换清单）见
> **[`docs/security-conventions.md`](docs/security-conventions.md)**。
> 提交前过闸门，命中即非 0 退出：
>
> ```bash
> python tools/gates/install_hooks.py           # 一次性：挂上 pre-commit 钩子
> python tools/gates/check_leaks.py             # 手动全量扫描（默认扫全部历史）
> python tools/gates/selftest_check_leaks.py    # 验证闸门**真的会拦**（变异测试）
> ```
>
> ⚠️ `run.py` 的槽位预检表**会**打印出口 IP（那是它的用途：按出口看额度）。
> 这段输出**不要粘进任何仓库、issue 或对话**。

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

# 5. 无头模式（不弹窗口）—— **已是默认**，无需加参数
python run.py --count 6

#    要弹窗口肉眼看流程时才用：
python run.py --count 6 --headful

# 6. 保存过程截图（排查用）
python run.py --shot debug

# 7. 规模化：起槽位代理池 —— 一次注册多个账号、每个走不同出口 IP
#    （本机出口 IP 已被注册封禁时必须走这条，见「槽位代理池」一节）
python tools/ops/gen_mihomo_slots.py --sub <订阅名> --slots 6 --filter 美国
python tools/ops/proxypool_ctl.py start  # 起独立 mihomo 实例（status/stop 同源）
python tools/probes/probe_slots.py          # 先量出真实出口 IP 个数 = 并发上限
echo 'IR_PROXY_SLOTS_FILE=.workbuddy-ai/proxypool/slots.txt' >> .env
python run.py --count 4 --workers 2          # 默认无头；要弹窗口加 --headful
python tools/ops/proxypool_ctl.py stop   # 用完停掉

# 8. 改代码前后（质量门）
python -m pytest                         # 行为测试（⚠ 别加 -q，见 tests/ 一节）
python -m ruff check .                   # 静态检查（配置在 pyproject.toml）
python tools/gates/check_leaks.py        # 提交前：泄漏闸门
python tools/gates/selftest_check_leaks.py   # 验证闸门**真的会拦**（变异测试，只用 stdlib）
```

> 🔴 **凭据一律走 `.env` 或环境变量**，代码里没有任何硬编码 token。
> 缺少必填项时 `run.py` 会在启动阶段直接报错退出（退出码 1），
> 而不是跑到一半才发现全是 401。`.env` 已在 `.gitignore` 中，不会进仓库。
> 环境变量优先级高于 `.env`，临时覆盖可直接 `IR_XXX=1 python run.py`。

本机 Chrome 路径默认 `C:\Program Files\Google\Chrome\Application\chrome.exe`，
可通过环境变量 `IR_CHROME_PATH` 覆盖。

### 环境变量

**凭据（必填）**

| 变量 | 默认 | 说明 |
|------|------|------|
| `IR_WORKER_ADMIN_TOKEN` | **无 —— 必填** | CF Worker 的 Admin Token。缺失时启动阶段即报错退出 |
| `IR_WORKER_BASE` | **无 —— 必填** | 临时邮箱 Worker 地址，形如 `https://<worker>.<subdomain>.workers.dev` |
| `IR_WORKER_DOMAIN` | **无 —— 必填** | 建邮箱使用的域名（须在该 Worker 的域名列表里） |

**可选（都有实测默认值，通常不用动）**

| 变量 | 默认 | 说明 |
|------|------|------|
| `IR_CHROME_PATH` | 本机 Chrome | 浏览器可执行文件 |
| `IR_CHAT_API_BASE` | `https://discovery-api.intern-ai.org.cn/v1` | 推理网关 |
| `IR_MICRO_BUDGET` | `45` | 鼠标喂数据预算（秒）。**只能往大调**，往小调会稳定拿到更差的 Path B，见下 |
| `IR_TYPE_DELAY_LO` / `_HI` | `45` / `110` | 逐字输入的按键间隔（毫秒）。**已实测调小净收益仅 0.5s**，见下 |
| `IR_NO_MICRO_MOVE` | 未设 | 置 `1` 关闭鼠标微移动（**仅用于对照实验**，生产不要开） |
| `IR_PREWARM_MS` | `0` | `goto` 后闲置 N 毫秒再操作（**仅用于对照实验**，见 [`docs/protocol.md`](docs/protocol.md)「captcha_wait 的方差来源」） |

**配额保护（本地累计计数，见「注册配额」一节）**

| 变量 | 默认 | 说明 |
|------|------|------|
| `IR_REG_QUOTA_MAX` | `40` | 滚动窗口内**成功注册**上限。撞到就停下，不再发请求 |
| `IR_REG_QUOTA_WINDOW_H` | `24` | 滚动窗口长度（小时）。实测恢复窗口 **> 8.6h**，取 24h 作保守上界 |
| `IR_QUOTA_STATE` | `.workbuddy-ai/state/register_quota.jsonl` | 计数文件路径覆盖（自检脚本靠它做隔离） |

对应的 CLI 开关：`--ignore-quota`（跳过本地保护，**仅当确信服务端已恢复**时用）。

**出口代理（换 IP 绕开 IP 维度的封禁，见「换出口 IP」一节）**

| 变量 | 默认 | 说明 |
|------|------|------|
| `IR_PROXY` | 未设 | `host:port:user:pass` 或 `scheme://user:pass@host:port`。作用于 **sso / discovery** |
| `IR_PROXY_MAIL` | 未设 | 置 `1` 时**邮箱 Worker 也走代理**（默认直连，因为收信轮询是瓶颈） |

选代理前先跑 `python tools/probes/probe_proxy.py host:port:user:pass` —— 三个实测坑
（TCP 连通不算数 / 状态码不算数 / 必须试真实目标域名）见该节。

**槽位代理池（规模化：一次注册多个账号，每个走不同出口 IP，见「槽位代理池」一节）**

| 变量 | 默认 | 说明 |
|------|------|------|
| `IR_PROXY_SLOTS_FILE` | 未设 | 槽位清单文件（**优先**）。一行一个或逗号分隔，`#` 注释。槽位多时用这个 |
| `IR_PROXY_SLOTS` | 未设 | 槽位清单，逗号分隔。适合少量槽位 |
| `IR_PROXY_COOLDOWN` | `120` | 槽位被判"出口被封"后的冷却秒数（**不是**永久拉黑） |
| `IR_PROXY_COOLDOWN_MAX` | `21600` | 冷却退避的封顶（6h）。同一槽位反复被封时按 2 的幂递增 |
| `IR_PROXY_SLOT_TIMEOUT` | `240` | 全池冷却时一个任务最多等多久，超时按失败记账 |
| `IR_PROXY_STATE` | `.workbuddy-ai/state/proxypool.json` | 池子状态文件路径覆盖（冷却 + 封禁次数跨运行保留，见下） |
| `IR_PROXY_PREFLIGHT` | `1` | 起飞前做槽位端口连通检查。置 `0` 跳过（离线自检必须跳） |
| `IR_SLOT_EGRESS_IPS` | 未设 | `端口=出口IP` 映射。齐了才启用**同出口互斥**，见「槽位代理池」 |

两个都没配 → `build_pool()` 返回 `None`，退回单代理行为（**不改变旧行为**）。
配了之后 `run.py` 会打印 `🔀 槽位代理池已启用`，并且**跳过全局配额守卫**
（本地计数是"老出口"的，改按槽位分别计）。
配完先跑 `python tools/probes/probe_slots.py` —— 它会把**去重后的真实出口 IP 个数**
报出来，**那个数才是并发上限**（实测 6 个槽位只有 4 个不同出口）。

### 🔴 池子状态会落盘：冷却与封禁次数**跨运行保留**

`.workbuddy-ai/state/proxypool.json` 存"哪些出口在冷却、被封过几次"，
建池时读回。**不保留的后果**：封禁退避是 120s → 240s → … → 6h，
而服务端配额窗口是 **24h** —— 进程一退退避就重置回 120s，
等于每重跑一次批量就在同一个被封的出口上重新撞一遍（一天约 720 次）。

三条设计约束（改这个文件前先读）：

1. **时间用墙钟 `time.time()`，不是 `time.monotonic()`** —— 状态要跨进程读写，
   而 monotonic 的原点是进程启动时刻，两个进程之间没有可比性。
   （`acquire()` 的等待超时仍是 monotonic，那是进程内时长；两套钟不混算。）
2. **键是 `host:port`，不是槽位位置号** —— `slots.txt` 增删一条会让位置号整体平移，
   冷却会静默错配到别的出口头上（同 `IR_SLOT_EGRESS_IPS` 那个坑）。用 `host:port`
   也顺带避免了把槽位串里的代理账密写进文件。
3. **读不出来就降级，不抛** —— 坏掉的状态文件不该让整批跑不起来。
   最坏后果只是退避从第一档重来。

租约（`_free` / `_ip_held`）与均衡计数（`_uses`）**不落盘** —— 前者是进程内的东西，
后者从 0 重来无危害。


## 架构

| 阶段 | 方式 | 说明 |
|------|------|------|
| 1. 注册 | 纯 HTTP | `POST /register/byEmail`，**无需人机验证** |
| 2. 激活 | 纯 HTTP | Worker 收信 → `extracted_json` 直接给出激活链接 |
| 3. 登录 | **浏览器** | 阿里云验证码 2.0，纯 HTTP 无法通过 |
| 4. 查额度 | 纯 HTTP | `getUserInfo` / `free-grant-status` / `balance` / `list_keys`（**只读**） |
| 5. 建 Key | 纯 HTTP | discovery tokenplan 接口（**需 `Idempotency-Key`**） |
| 6. 验证 | 纯 HTTP | 真发一次 `chat/completions` 确认 key **真能用**（非致命） |

> Stage 3~6 可以用 `tools/run_downstream.py` **单独**对已有账号跑通，
> 不需要重新注册 —— 详见「注册被封时怎么继续干活（二）」。

**出口 IP 分配（Stage 1+2 用）**：`src/proxypool.py` 的槽位池给每个 producer
一个独立出口 IP。封禁是 IP 维度，所以**这一层决定"一次能注册几个"**，
`workers` / `reg_concurrency` 都不是。没配 `IR_PROXY_SLOTS*` 时它整个不存在
（`build_pool()` 返回 `None`），行为与加它之前完全一致。

### 项目结构

```
run.py                 CLI 入口（含启动配置校验）
requirements.txt       运行依赖
pyproject.toml         工具链配置（ruff + pytest；`pythonpath = ["."]` 让 tests/ 直接 import src）
.env.example           凭据模板（复制为 .env 后填值）
.gitignore             排除 .env / 运行产物 / .workbuddy-ai

src/
  config.py            配置与常量（.env 加载、启动校验、模型清单）
  crypto_rsa.py        RSA 密码加密（复刻前端逻辑）
  tempmail.py          CF Worker 临时邮箱客户端（自适应轮询窗口）
  sso.py               SSO 注册 / 激活
  browser/             浏览器登录子包 —— 2026-09-19 从 `browser_login.py`(955 行) 拆出
                        🔴 **函数体逐字节未改**，只搬位置；等价性由
                           `.workbuddy-ai/tmp/verify_stage_b_split.py` 复算（32/32 定义）
    __init__.py        包 docstring（反检测 / 风控 / 两条验证码通路的实测结论）+ 公共 API re-export
    constants.py       10 个可调常量（环境变量覆盖）
                       ⚠ 各模块 `from .constants import X` 绑的是**副本** ——
                         patch 要打在**读它的那个模块**上，打包级属性会**静默失效**
    urls.py            `build_login_url()` —— 独立叶子，避免 attempt ↔ entry 循环导入
    state.py           `LoginResult`（对外契约，字段名与顺序被测试冻结）+ `_AttemptState`
    behavior.py        人类化鼠标轨迹（**风控真正评估的信号**，别为提速删掉）
    captcha.py         验证码勾选框点击与滑块探测
    attempt.py         一次尝试：8 个 `_step_*` + `_build_result` + `_run_attempt` 编排
    session.py         `_launch_kwargs`（`chrome_args=` 注入面）+ `_retry_loop` + `BrowserSession`
    entry.py           `login()` 单账号入口（不复用浏览器会话）
  discovery.py         discovery 平台（额度 / API Key）
  apikey.py            推理网关客户端（OpenAI 兼容）
  pipeline.py          端到端编排（两段式流水线 + `QuotaGovernor` 配额决策）
  quota.py             注册配额的本地累计计数与保护（见「注册配额」一节）
  proxypool.py         槽位代理池（一槽一端口 = 一个固定出口 IP，租约式分配 + 状态落盘）
  ledger.py            账号台账（`ledger/` 目录；读源 = 最新那份**全量**快照）的读写与合并
                       —— **所有会写台账的工具都必须用它**
  redact.py            脱敏助手（日志 / 输出边界必须过这里，见 docs/security-conventions.md）

tests/                 pytest 行为测试 —— 断言从已移除的 `tools/selftests/*.py` **保真迁移**而来
  conftest.py          autouse 夹具：运行态文件（配额台账 / 池子状态）重定向到 tmp；
                       台账夹具 `ledger_sample` / `real_ledger` / `any_ledger`
  fixtures/            测试数据（唯一入库的 `.json`：脱敏样本台账）
  test_proxypool.py    槽位池（均衡 / 冷却退避 / 同出口互斥 / 端口预检 / exclude / accept / 状态落盘）
  test_quota.py        配额计数（窗口 / 触顶等待 / 并发追加 / 补录 / scope 隔离）
  test_quota_governor.py 配额决策的**差分等价**（内嵌改造前的内联逻辑当参考实现）
  test_error_kind.py   错误结构化字段（打标点 / 读点 / 新旧判据的分歧清单）
  test_ledger_merge.py 台账合并（运行期）与防缩水护栏
  test_ledger_fragments.py 碎片合并（重建台账）—— 字段只增不减 / 降级补缺口 / 键序
  test_ledger_sample.py 脱敏样本自身的守卫（形状覆盖 / email 唯一 / 无真凭据形态）
  test_dependency_surface.py 元测试：测试链的第三方依赖必须 ⊆ ci.yml 装的那三个
  test_browser_login.py `src/browser/` 的**契约**（字段/键名冻结 + 重试循环 + 启动参数注入）
                        —— 零浏览器；**刻意从真源子模块导入**，私有函数不走包，
                           这样"旧路径还能用"的错觉会立刻变成 ImportError 而不是静默失效
  test_redact.py       脱敏边界（userinfo / 空串 / 密码含 @ / keep 语义）

  跑法：`python -m pytest`。CI 就只跑这个 + ruff（见 .github/workflows/ci.yml）。
  🔴 **不要加 `-q`**：`pyproject.toml` 的 `addopts` 已经有一个 `-q`，命令行再写一个
     会叠成 `-qq`，把汇总行（`239 passed in 5.39s`）吞掉 —— 日志里只剩一串点。
     要调详细程度请改 `addopts`（一处生效）。
  🔴 测试链的第三方依赖是 **`requests` + `cryptography`**，不是"零依赖"：
     `test_error_kind.py` → `src/pipeline.py` → `src/discovery.py` 要 requests；
     → `src/sso.py` → `src/crypto_rsa.py` 要 cryptography。CI 的 test job 必须装。
     ⚠ `playwright` **不在**收集路径上（`src/browser/session.py` 里是函数体内的
       延迟 import），CI 刻意不装 —— 这个边界要留住。
     这条假设已由 `test_dependency_surface.py` 钉成**可执行断言**（`ALLOWED`）：
     测试链上一旦多出新的第三方包，本地跑测试就红，不用等 CI。
     （2026-09-19 教训：这里原来写"零第三方依赖、CI 不用装运行依赖"，
       该错误假设让 CI 连续红了 3 次 —— 本地全绿只因为本地装过。）
  ⚠ 2026-09-19：原 `tools/selftests/*.py`（手搓断言框架，910 行）已移除 ——
     它是 tests/ 的**重复实现**。删除前提是"迁移保真"已被变异验证证明
     （改一处源码 → 新旧两套同时变红，漏测 0），见 docs/refactor-plan-2026-09-19.md §5.2。

tools/                 脚本按职责分 4 个子目录。**不是 Python 包**（没有 __init__.py）
  _bootstrap.py            把仓库根加进 sys.path（唯一实现）
  run_downstream.py        **第二个入口**：对已有账号跑下游全链路（登录→额度→建/复用Key→真推理），零注册请求

  probes/    13 个一次性诊断探针 —— 每个回答一个具体问题，**改代码前先取实测数据**
             先读 probes/README.md：一览表写清了"每个探针回答什么问题 / 什么时候跑 /
             看哪个数字 / 结论落在哪"。有 4 个的结论已被生产代码吸收，不必再跑。
    probe_429.py             注册限流边界（绕开退避重试打裸请求）
    probe_reg_interval.py    注册闸门间隔降序试探（见 429 边界表）
    probe_captcha_timing.py  验证码通路 / 微移动预算对照实验（见 [`docs/protocol.md`](docs/protocol.md)「验证码有两条通路」）
    probe_login_timing.py    登录时序：打字间隔 / 页面闲置对照实验（带事件时间线）
    probe_login_route.py     登录页是否存在"直达密码表单"路由
    probe_headless.py        无头模式可用性验证
    probe_env.py             浏览器环境指纹导出
    probe_quota_scope.py     封禁是 IP 维度还是邮箱域名维度（控制变量：只换域名）
    probe_proxy.py           代理能否用于本项目（出口 IP / 归属 / 目标域名可达性）
    probe_login_only.py      **只测登录**（用已有账号）—— 测 workers 天花板
    probe_balance.py         **用已存 JWT 查额度**（不开浏览器，0.5s 查 15 个账号）
    probe_slots.py           **探槽位池：去重后的真实出口 IP 个数 + 目标站可达性**
    probe_register_ip.py     **决定性实验**：只打注册一枪，判定封禁是不是 IP 维度

  gates/     泄漏闸门（命令见开头的指针块；规范见 docs/security-conventions.md）
    check_leaks.py           内容层扫描（默认扫全部 git 历史）
    install_hooks.py         一次性挂 pre-commit 钩子
    selftest_check_leaks.py  变异测试：验证闸门**真的会拦**，不是摆设

  ops/       运维（都带 --help）
    proxypool_ctl.py         **槽位实例的启停与体检**（按端口区分身份，绝不误杀 Clash Verge 主内核）
    gen_mihomo_slots.py      从订阅生成 N 槽位 mihomo 配置 + slots.txt（见「槽位代理池」）
    cf_service_doctor.py     CF Worker 体检
    check_keys_alive.py      检查 key 存活性（`/v1/models` 全量 + 抽样真实推理，含负对照）

  data/      台账读写（**全部经过 `ledger.py` 的合并入口**，不自己写 JSON）
    export_keys.py           导出历史 API Key（三重去重 + 输出目录 gitignore 校验 + 读自己的 CSV 自保）
    restore_results.py       从散落来源重建台账（合并规则用 `ledger.merge_fragments`）
    recover_activation.py    **补激活**：救回"注册成功但激活失败"的账号
    migrate_quota_scope.py   把老台账的配额计数迁到按出口 IP 记账
    prune_ledger.py          剪掉台账里**没有账号信息**的空记录（配额守卫中止的残渣）
```

> **怎么跑**：一律**从项目根**执行，例如 `python tools/probes/probe_429.py`。
> 子目录里的 `_path.py` 负责把 `tools/` 与仓库根加进 `sys.path`（脚本移进子目录后
> `sys.path[0]` 会变成子目录，直接 `from _bootstrap import ROOT` 会失效）。
> ⚠️ 别把它改名成 `_bootstrap.py` —— 那样会 import 到自己，报
> `cannot import name 'ROOT' from partially initialized module`。

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

**边界是实测出来的**（`tools/probes/probe_reg_interval.py`）——
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

### 🔴 `workers` 的边界：浏览器侧到 6 都放行，真正的墙是**注册配额**

#### 第一轮：整链批量（被注册配额污染）

实测 6 账号，`--headless`，同一次会话内连续跑：

| 配置 | 成功 | 关键路径 | 每账号 | 登录耗时分布（秒） |
|------|------|----------|--------|--------------------|
| `--workers 2` | 6/6 | 61.2s | 10.2s | 12.5 · 13.7 · 15.6 · 15.8 · 16.1 · 17.0 |
| `--workers 3` | 6/6 | **47.5s** | **7.9s** | 14.2 · 16.1 · 16.7 · 17.0 · 18.4 · 21.3 |
| `--workers 4` | **3/6** | — | — | 失败全在 **register** 阶段 |
| `--workers 6` | **0/6** | — | — | 同上，7.6s 内全灭 |

**`workers=4` 的失败与 workers 无关**：`reg_conc = min(count, REG_CONCURRENCY)`
恒为 4，改 `workers` 根本不影响注册并发度。真实原因是**注册配额触顶**（见下节）。
→ **这一轮没法回答"浏览器侧能并发到几"**，因为它被注册配额盖住了。

#### 第二轮：只测登录（隔离掉注册配额）

注册被 IP 封掉后，改用**已有账号只测登录**（`tools/probes/probe_login_only.py`）——
登录不消耗注册配额，于是浏览器侧的天花板终于能被单独观测：

| workers | 账号数 | 总耗时 | 每账号 | 单账号中位 | 最慢 | 失败 |
|---------|--------|--------|--------|------------|------|------|
| 1 | 5 | 82.2s | 16.4s | 16.2s | 18.7s | 0 |
| 2 | 6 | 61.1s | 10.2s | 17.2s | 23.9s | 0 |
| 2 | 6 | 83.4s | 13.9s | 16.1s | **51.2s** | 0 |
| 3 | 6 | 37.2s | 6.2s | 17.7s | 18.9s | 0 |
| 3 | 6 | 56.9s | 9.5s | 22.7s | 31.2s | 0 |
| 4 | 6 | 42.3s | 7.1s | 20.8s | 23.9s | 0 |
| 6 | 6 | 24.6s | 4.1s | 20.7s | 23.2s | 0 |
| 6 | 12 | 43.7s | 3.6s | 19.2s | 21.6s | 0 |
| 8 | 8 | 24.1s | 3.0s | 21.9s | 22.3s | 0 |
| **12** | **12** | **29.2s** | **2.4s** | 25.0s | 27.2s | **0** |

**结论**：

1. **并发到 12 都零失败、零 `F001`** —— 浏览器侧**不是**瓶颈，早期"风控放行到 3"
   的结论是错的（那个 3 是被注册配额间接限出来的）。
2. **吞吐单调改善，但单账号耗时在退化**（次线性扩展）：
   中位 16.2s（顺序）→ 21.9s（8 路）→ 25.0s（12 路），涨了 54%，
   因为 12 个 Chrome 在 12 逻辑核心上争 CPU。**净效果仍是赚**：
   16.4s/账号 → **2.4s/账号**，**6.8 倍**。
3. **没有测到明确的拐点** —— 12 路仍在改善（2.4s < 8 路的 3.0s）。
   要更高并发建议先在目标机器上按本表复测，别照搬。
4. `12 账号 / 6 路 = 2 轮` 那一行证明多路**跨轮可持续**，不是"只跑一轮才好看"。
5. **总时长由最慢账号决定**：`workers=2` 第二轮出现一个 **51.2s** 的离群值，
   直接把该轮总时长从 61.1s 推到 83.4s。同一配置两次跑差 36%，
   全是这一个离群值造成的 —— 这就是为什么报告里打印的是**最慢账号**的分解。

默认取 **4**（落在已验证区间内，且比原来的 2 快一倍）。
`--workers 6~12` 已实测安全，但**只在只测登录时验证过** —— 注册被 IP 封着，
整链在 6 路以上尚未复测。小批量时让 `workers ≈ count`，否则最后一轮有 worker 空转。

> ⚠ 这一节**推翻了本项目早期的两个结论**：
> ① "workers=3 更慢"（`form_ready` 1.78s → 11.53s）—— 那是旧配置 + 本机负载
>    不同时的数据，换配置后方向完全相反；
> ② "并发上限是本机渲染能力 / 风控放行到 3" —— 实测到 12 全放行。
> → **"以前测过"不等于"现在仍然成立"**，而且**被测指标的混杂因素没排除时，
>   测出来的"上限"可能是别的东西的上限**。

### 🔴 注册配额是**累计量**限制，不是瞬时速率

`REG_MIN_INTERVAL` 只管住了**瞬时速率**（两次 `register/byEmail` 之间至少 1.2s）。
平台还有一层**累计配额**：

| 时段内累计注册 | 现象 |
|---|---|
| 0 ~ 约 40 个 | 全部成功 |
| 约 40 个之后 | 开始零星 `B0000 请求频繁，请稍后再试` |
| 再往后 | 连**单账号**都注册不了 |

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

#### 🔴 修正一：恢复窗口 **> 8.6 小时**（"数分钟"是错的）

最初写下的是"等待数分钟仍未恢复"，据此给本地窗口取了 6h。**当晚就被推翻**：

```
14:05:43   最后一次成功注册
22:39:00   同一台机器、单账号探测 → 仍然 B0000     ← 8.6 小时后
```

所以恢复窗口 **> 8.6h**。原来那个 6h 窗口**不是"保守"，而是"太短"** ——
它会在服务端仍封着时放行，白跑一轮。现在 `IR_REG_QUOTA_WINDOW_H=24`。

#### 🔴 修正二：封禁是 **IP 维度**，不是邮箱域名维度

一个自然的猜测是"某个发信域名被平台拉黑了"。Worker 支持 **多个域名**，
于是可以做**控制变量实验**（同机器、同 IP、同一套请求头，**只改邮箱域名**）：

```
<域名 A>    → HTTP 200  {"traceId":..., "msgCode":"B0000", ...}
<域名 B>    → HTTP 200  {"traceId":..., "msgCode":"B0000", ...}   ← 换域名无效
```

→ **换发信域名没用**，只能等窗口或换出口 IP。`tools/probes/probe_quota_scope.py` 可复现。

#### 🔴 换出口 IP 才是对症解法，但要先查清 IP 的"类型"

既然封禁是 IP 维度，换 IP 就是解法。但**不是随便一个代理都能用** ——
2026-09-16 实测一组代理，踩了四个坑，全部记录如下。

**① 先看现在用的是什么样的 IP**

```
直连出口 203.0.113.20    → 美国/洛杉矶 · NTT America / 天风通信（香港）
                           hosting=false proxy=false，但归属是**机房**
```

→ 我们一直从**机房 IP** 发注册。这类 IP 段被大量自动化共享，信誉最差，
很可能就是被封的原因。**住宅 IP（家宽）信誉高得多**。

**② TCP 连通性完全不能作为判据**

本机跑着 Clash Verge（TUN 模式 + fake-ip），于是：

```python
socket.connect(("203.0.113.30", 764))   # → 0.02s "连通"
```

中国到美国**不可能 20ms**。那是连到了**本地虚拟网卡**（`198.18.0.0/15`
是 TUN 的 fake-ip 段），真正的连接还没建立。
→ 必须真的发一个 HTTP 请求、**拿到出口 IP** 才算数。

**③ 状态码不能作为判据，要看响应正文**

代理的域名 ACL 拒绝转发时返回 `403`，但**正文才是判据**：

```
HTTP 403   Proxy-Authenticate: Basic real=""
正文:      errorMsg: sso.openxlab.org.cn:80 not accessible
```

只看 403 会以为"目标站返回了 403"，实际是**代理自己拒绝转发**。

**④ 必须用真实目标域名试，不能拿通用站点代跑**

这组代理能通 google / baidu / github / taobao / aliyun，却精确屏蔽了：

| 域名 | 80 | 443 |
|------|----|-----|
| `sso.openxlab.org.cn` | ⛔ `not accessible` | ✗ TLS 被掐断 |
| `discovery-api.intern-ai.org.cn` | ⛔ 同上 | ✗ 同上 |
| `openxlab.org.cn` / `intern-ai.org.cn` | ⛔ 同上 | ✗ 同上 |
| `www.qq.com` | ⛔ 同上 | ✗ 同上 |
| taobao / 163 / baidu / aliyun / github / google | ✓ | ✓ |

→ 这是**精确的域名黑名单**，不是"中国站点被拦"。三个端口（三个不同出口 IP：
`203.0.113.41` Google Fiber / `203.0.113.42` Spectrum，都是美国住宅）
**拦截完全一致** —— 说明**换出口 IP 也没用，黑名单在服务商侧**。

443 的表现和 80 不同：代理是**直接掐断 TLS**（只拿到
`UNEXPECTED_EOF_WHILE_READING`，没有正文）。所以探测器会在 443 失败后
**用 80 端口回退**去问那句明确的拒绝原因 —— 否则只能报"不通"，
而"被代理拦截"和"网络不通"的处置方式完全不同。

**怎么用**（`tools/probes/probe_proxy.py`）：

```bash
python tools/probes/probe_proxy.py host:port:user:pass [更多...]
python tools/probes/probe_proxy.py --file proxies.txt        # 每行一条
```

输出三档判定：`可用` / `被代理拦截` / `不通`，并给出出口 IP 的
国家/城市/ISP 与"住宅 or 机房"倾向。可用的会直接打印出该设的 `IR_PROXY`。

**接进流水线**：

```bash
IR_PROXY=host:port:user:pass python run.py --count 5
```

| 变量 | 作用 |
|------|------|
| `IR_PROXY` | 出口代理，作用于 **sso / discovery**（被封的那一侧） |
| `IR_PROXY_MAIL` | 设为 `1` 时**邮箱 Worker 也走代理**（默认直连） |

邮箱默认直连是有意的：收信轮询是整链瓶颈（激活邮件到达就要 6.02s），
绕道代理只会更慢；Cloudflare Worker 也不关心我们的出口 IP。

**🔴 `session.proxies` 会被环境变量静默盖掉（本机踩中）**

本机有 `HTTP_PROXY=http://127.0.0.1:7897`（Clash）。此时：

| 写法 | 结果 |
|------|------|
| `session.proxies = {...}` | ✗ **被忽略**，走了 Clash —— 请求还是 200 |
| `session.proxies = {...}` + `trust_env = False` | ✓ 生效 |
| `session.get(url, proxies={...})` | ✓ 生效（per-request 优先级最高） |

机制在 `requests/sessions.py` 的 `merge_environment_settings`：

```python
env_proxies = get_environ_proxies(url)          # 先读环境变量
for k, v in env_proxies.items():
    proxies.setdefault(k, v)                    # ← 环境变量先进字典
proxies = merge_setting(proxies, self.proxies)  # ← 已存在的键不覆盖
```

→ `session.proxies` 的优先级**低于**环境变量。现象特别隐蔽：
请求**成功了**（200），只是**没走你指定的代理**，既不报错也不告警。
`config.apply_proxy()` 因此会同时关掉 `trust_env`；未配置 `IR_PROXY`
时则**不动** `trust_env`，保持原有行为（环境代理仍是默认出口）。

#### 🔀 规模化落地：槽位代理池（一条出口不够，要**多条**）

上面那条 `IR_PROXY` 只有**一条**出口 —— 整个批次共用同一个 IP。
也就是说 `workers` 调到几都一样：**IP 维度的累计配额是共享的**，撞上是必然。
真正的约束是"**有多少个不同出口 IP**"，不是本地并发度。

设计参考 `asz798838958/aBaiFreeGPT` 的代理池：mihomo 里一个节点 = 一条
`listeners[]` = 一个本地端口 = 一个固定出口 IP；程序侧按**租约**把端口分给 worker。

```
订阅(机场) ──> tools/ops/gen_mihomo_slots.py ──> .workbuddy-ai/proxypool/config.yaml
                                              + slots.txt（6 条 http://127.0.0.1:790N）
                                                        │
独立 mihomo 实例 ──监听 7901..7906───────────────────────┘
                                                        │
run.py ──> run_batch ──> build_pool() ──acquire()──> worker 独占一个出口 IP
                              ▲                      │
                              └──release/report_banned/report_failed──┘
```

**三步起池**

```bash
# 1) 生成槽位配置（顺带写出 slots.txt）
python tools/ops/gen_mihomo_slots.py --sub <订阅名> --slots 6 --filter 美国

# 2) 起**独立** mihomo 实例（别动 Clash Verge —— 它把 TCP 控制器关了，
#    而且改它的配置会影响你正常上网）
python tools/ops/proxypool_ctl.py start        # 推荐：带身份校验 + 端口复查
# 等价于手写：
#   "<你的 mihomo 可执行文件>" \
#       -d .workbuddy-ai/proxypool -f .workbuddy-ai/proxypool/config.yaml

# 3) **先探测再跑**（必做，见下）
python tools/probes/probe_slots.py
```

**🔴 停实例 / 体检：`python tools/ops/proxypool_ctl.py status|stop|start|restart`**

**这台机器上同时跑着 3 个 `verge-mihomo.exe`，而且它们的可执行文件路径、
进程名完全一样**：

```
26952  0.0.0.0:1053 + 127.0.0.1:7897      ← Clash Verge 主内核（你上网靠它）
11156  127.0.0.1:7901-7906, 17901/17902   ← 本项目的槽位实例
35588  127.0.0.1:7911-7913, 17911/17912   ← 本项目的对照实验实例
```

所以 **"按进程名 kill"是必错的做法** —— 杀到主内核就直接断网。
唯一可靠的区分方式是**看它监听哪些端口**：持有 `7897` / `1053` 的一律不碰。

`proxypool_ctl.py` 把这个判断固化了，并且有三道保险：

1. `status` 用端口给每个进程**分类**（🔴 主内核 / 🔀 我们的 / ⚪ 其它）
2. `stop` 在动手**前**再 `force` 查一次端口，持有主内核端口就**放弃**
3. 停完**双向复查**：端口真的释放了吗？主内核真的还在吗？

```
🛡  保护（不会碰）:
     PID 26952  verge-mihomo.exe  持有主内核端口 [1053, 7897] —— **用户上网靠它**

🔀 准备停止:
     PID 31972  端口 [7911, 7912, 7913, 17911, 17912]
     ✓ PID 31972 已停止

已停 1 个，失败 0 个
   端口复查：✓ 全部释放
🛡  主内核复查：✓ 仍持有 [1053, 7897]（PID [26952]）
```

> **⚠ 常驻性**：`start` 用 `DETACHED_PROCESS` 拉起进程，在**你自己的终端**里
> 跑是能常驻的。但如果是在受管沙箱里执行（进程挂在 Job Object 下），
> 进程会**随这次调用结束被回收** —— 表现是「同一次调用里 `status` 看得到，
> 下一次调用就没了」（实测踩到，一开始还以为是工具写错了）。
> 那种环境要么用宿主提供的后台执行能力起，要么在自己的终端窗口里跑。
>
> 判断它还在不在：`python tools/ops/proxypool_ctl.py status`
> 或 `netstat -ano | grep -E "790[0-9]"`（有 `LISTENING` 就活着）。
> **它和 Clash Verge 是两个进程、两份配置，互不干扰** —— 别指望 Clash Verge
> 顺手带上它，也别去改 Clash Verge 的配置。
>
> 未配置槽位时 `build_pool()` 返回 `None`，`run_batch` 走原来的单代理路径 ——
> **这个功能不改变没配它的人的运行结果**。

然后 `.env` 里加一行就自动启用：

```bash
IR_PROXY_SLOTS_FILE=.workbuddy-ai/proxypool/slots.txt
```

**🔴 坑一：不同节点名 ≠ 不同出口 IP（这条最关键）**

实测 6 个槽位（6 个不同节点名）只得到 **4 个**不同出口 IP：

```
SLOT-01 (7901) -> 203.0.113.11
SLOT-02 (7902) -> 203.0.113.12
SLOT-03 (7903) -> 203.0.113.13
SLOT-04 (7904) -> 203.0.113.14
SLOT-05 (7905) -> 203.0.113.13   ← 与 03 重复
SLOT-06 (7906) -> 203.0.113.11   ← 与 01 重复
```

同机房的多台机器常常共用同一个 NAT 出口。所以**"我配了 6 个槽位"不等于
"我能并发 6 个账号"** —— 真实上限是**去重后的出口 IP 个数**。
`tools/probes/probe_slots.py` 就是量这个数的：

```
槽位总数      : 6
**不同出口 IP**: 4   ← 这才是能同时用的账号数上限
  203.0.113.11      2 个槽位: 7901, 7906  ⚠ 2 个槽位共用同一个出口
  203.0.113.13      2 个槽位: 7903, 7905  ⚠ 2 个槽位共用同一个出口
  ...
✅ 既拿得到出口 IP、又能到目标站的**不同出口**: 4 个
```

配 6 个槽位就往 `workers=6` 冲，只会让多出来的槽位在同一个出口上排队，
**反而更快撞穿那个 IP 的配额**。

**🔴 坑二：`curl --noproxy '*'` 会把 `-x` 指定的代理一起禁掉**

测槽位出口时一度得到"6 个槽位全是 `203.0.113.20`（直连出口）"，
看起来像"代理没生效"。真实原因是 `--noproxy '*'` 的语义是**禁用一切代理**，
包括 `-x` 指定的那个 —— 测的其实是直连。
→ 去掉 `--noproxy '*'` 后立刻正常。**这条已写进 `probe_register_ip.py`
的 `exit_ip()` docstring 当警告。**

**🔴 坑三：判据必须按**真实出口 IP**去重，不是按槽位号**

`tools/probes/probe_register_ip.py` 只打注册一枪、不激活不建 key，用来做决定性实验。
它的做法是**先探所有出口 IP、按真实 IP 去重、再逐个打枪** ——
按槽位号去重会把同一个出口打两次，得出"换 IP 也没用"的错误结论。

**实测结论（2026-09-18，封禁已持续 68h+）**

```
出口 203.0.113.11   → ✅ 注册成功
出口 203.0.113.12   → ✅ 注册成功
出口 203.0.113.13   → ✅ 注册成功
成功 3   配额封禁 0   其他 0
```

**3/3 成功** —— 封禁确实只是 IP 维度，换出口 IP 即解开。
随后用槽位池跑真实批量（`--count 4 --workers 2`）：**注册 4/4 全部成功**，
每个账号落在不同出口（`slot1..slot4`）。

**三条租约规则**（照搬参考实现，理由见 `src/proxypool.py`）

| 规则 | 做法 | 为什么 |
|------|------|--------|
| 租约**粘性** | 一个 worker 从注册到激活走同一个出口 | 中途换 IP 会让服务端看到"半个会话换了来源" |
| 被封进**冷却**，不是永久拉黑 | `report_banned` 默认 120s | 永久拉黑会让池子越跑越小，最后全不可用 |
| **均衡分配** | 排序键 `(累计使用次数, 槽位号)` | 简单轮询会让少数出口被反复用，更快撞穿它的配额 |

冷却时长**必须分档**：被封（`B0000`）用长冷却 120s，网络类失败用短冷却 20s。
混成一种的后果：要么把健康出口按长冷却晾 2 分钟（吞吐塌），要么把被封出口
当偶发故障 20s 后重投（继续撞墙）。

**🔴 槽位模式下的两个连带修正**

1. **本地配额计数必须按出口分开算**（`src/quota.py` 的 `scope`）。
   本地计数是按"本机出口 IP"记的，槽位池有多个出口 —— 加在一起算是没意义的数
   （6 个出口各用 10 个，全局看 `60/40` "超额"，实际每个都很空）。
   老记录（没有 `scope` 字段）属于**换出口之前**那个 IP，按 scope 过滤时
   天然不参与 —— 这正是我们要的：换了出口，计数就该从头算。
2. **`B0000` 不再中断整批**。旧逻辑连续 2 个 `B0000` 就把整批标 skipped ——
   那是单出口的假设。槽位模式下 `B0000` 只代表**那个出口**被封，
   把还有余量的出口一起停掉是错的。自我保护改由槽位冷却承担；
   若所有出口都被封，`acquire()` 会阻塞到超时（`IR_PROXY_SLOT_TIMEOUT`），
   效果等价于"停下来"，但不会误伤干净的出口。

**自测**：`python -m pytest tests/test_proxypool.py`（48 项，零网络）——
覆盖均衡分配、全忙阻塞超时、重复归还幂等、长/短冷却分档、冷却到期自动恢复、
`exclude` 向前推进、4 线程并发不重不漏、状态落盘与跨池续退避。

> 原 `tools/selftests/selftest_proxypool.py`（手搓框架，36 项）已于 2026-09-19 移除 ——
> 它是这个文件的**重复实现**。移除前用变异验证确认过覆盖等价
> （改一处源码 → 新旧两套同时变红，漏测 0）。

#### 🔴 修正三：**两层限流是两套不同的系统**，同秒连发会盖掉真实结果

第一次跑上面那个实验时，两个域名**在同一秒**各发一次，第二个拿到：

```json
HTTP 429   {"error": "Too many request"}                     server: istio-envoy
```

而 B0000 长这样：

```json
HTTP 200   {"traceId": "...", "msgCode": "B0000", "success": false}
```

**这是两层，不是同一个限制的两种表现**：

| | 响应 | 判定层 | 管什么 | 时间尺度 |
|---|---|---|---|---|
| 突发 | `HTTP 429 {"error":...}`（无 `traceId`，`server: istio-envoy`） | **网关层** | 瞬时突发 | 秒级 |
| 累计 | `HTTP 200 {"msgCode":"B0000"}`（有 `traceId`） | **应用层** | 累计量 | 小时/天 |

同秒连发会**稳定命中网关层**，把应用层的真实结果盖掉 → **实验无效**。
所以探针必须逐次重试并留间隔，且**只有拿到 `HTTP 200` 的响应才算对应用层下了判断**。

> **通用判据**：看到 429 和看到业务错误码时，先问一句
> **"这是同一层给的，还是两层不同系统？"**
> 判据是**响应体形状**（有没有 `traceId`）和**响应头**（`server:` 是谁），
> 不是状态码。把两层混成一层，实验设计和结论都会歪。

#### 对策：本地累计计数 + 三层保护（`src/quota.py`）

**恢复窗口未知** → 与其继续试探，不如自己记一份账，在接近上限时主动停。
`src/quota.py` 把每次**成功**注册追加到 `.workbuddy-ai/state/register_quota.jsonl`
（JSONL 追加写，多 producer 线程并发安全；失败不计数，记了会虚高），
然后在三个位置生效：

| 层 | 位置 | 作用 |
|----|------|------|
| ① 开跑前 | `run_batch` 入口 | 窗口已满 → 抛 `QuotaExceeded`，**一个请求都不发**（退出码 2）；余量不足 → 打印后把计划量**裁到剩余** |
| ② 运行中 | `stage_register` 限速闸门**之后** | 连续 2 个 `B0000` → 置位停止标志，后续任务标 `skipped`（**未发请求**） |
| ③ 报告 | `run.py` 收尾 | 打印窗口用量；`skipped` 与 `failed` 分开统计，不混为一谈 |

**🔴 第 ② 层的检查位置是实测逼出来的**，放在 `producer` 开头**不够**：

`ThreadPoolExecutor` 会把 `min(count, reg_conc)` 个任务**同时**启动，它们会在
**任何失败发生之前**一起通过检查。实测 `count=4 / reg_conc=4`：

```
检查放 producer 开头  →  4 个 B0000、0 个 skipped   ✗ 保护完全没生效
检查放限速闸门之后    →  2 个 B0000、2 个 skipped   ✓
```

`gate()` 是**全局串行点**（所有 producer 在那里排队），只有放在它后面才保证
"看到前序请求的结果"。→ 通用教训：**停止检查必须放在串行点之后，放在并发窗口
之前只能挡住尚未启动的任务。**

**代码位置**：账本住在 `src/quota.py`（窗口计数 + 补录），三处决策点收在
`src/pipeline.py::QuotaGovernor`：

| 方法 | 对应层 | 职责 |
|------|--------|------|
| `allow(count)` | ① 开跑前 | 窗口已满 → 抛 `QuotaExceeded`；余量不足 → 裁剪计划量并返回新值 |
| `check_slot(idx)` | ② 运行中 | 给 `ThreadPoolExecutor` 的 accept 谓词用（**在串行点之后**） |
| `claim_slot(scope)` | ② 运行中 | 拿到槽位租约后复查，返回跳过原因或 `""` |
| `note_result(ok, rec)` | ② 运行中 | 结果哨兵：连续 2 个配额证据 → 置位停止标志 |
| `hit()` / `streak()` | ③ 报告 | 供收尾统计读取 |

这只是把原先散在 `run_batch` 里的内联逻辑**收拢成类，判据一字未改** ——
`tests/test_quota_governor.py` 用**差分等价测试**钉住这一点：把改造前从 `HEAD`
逐字抄下来的内联实现当参考实现（`_ref_*`），对同输入逐项断言新旧一致，
另有一条用例**刻意钉住已知分歧**（守卫文案含 `B0000` 的两种形状），
并用 AST 断言这些分歧形状**当前不可达** `settle_lease()`。

**验证**（`python -m pytest tests/test_quota.py` + 端到端三路径）：

| 验证 | 结果 |
|------|------|
| 模块测试（计数 / 两种 `check_or_raise` / 并发 append / 坏行 / 窗口 / 压缩 / 不可写 / 补录 / **超额等待**） | 全绿（`tests/test_quota.py`） |
| 20 线程 × 5 次并发 append | **100 行零丢失**（这是最要紧的一条 —— 丢了就是静默放行） |
| T1 窗口已满 → `--count 1` | 退出码 **2**，耗时 0.3s，**零网络请求**（state 行数不变，实测） |
| T2 余量 1/3、计划 5 → | 裁剪为 **2**，打印裁剪原因，结果恰 2 条 |
| T3 计划 4、运行中触顶 | **2 failed + 2 skipped**，skip 的确实没发请求 |
| 补录幂等性 | 连跑两次 → 第二次 `新增 0 / 重复 53` |

**🔴 "还要等多久"不能按"最早一条滑出"算**（自检补出来的真 bug）：

补录会把计数一次性**推过上限** —— 本项目补录后是 `53/40`。这种状态下
"最早一条滑出窗口"只让计数从 53 降到 52，**守卫仍然拦着**；真正要等到第
`used - limit + 1` 条滑出：

```python
must_expire = max(used - limit + 1, 0)     # 未触顶时为 0
```

旧实现只取 `oldest_ts`，在真实 state 上报 **"169.5 分钟后可再注册"**，
手算真实值是 **213.7 分钟** —— 少报 44 分钟，而且到点后依然是拦着的。
`used == limit` 时两者恰好相等，所以只有 `used > limit` 才暴露。

→ 通用教训：**"还剩多久可用"这类量，阈值比较必须考虑"当前值已越过阈值"的情况**；
只在边界点上验一次是验不出来的（旧自检只覆盖 `used == limit`，所以 37/37 全绿却带着 bug）。

**⚠ 边界**：这是**本地保护，不是权威计量** —— 换机器、删 state 文件都会重置；
`REG_QUOTA_MAX=40` 是实测触顶点的估计值，服务端真实阈值可能更低（所以第 ② 层
是必要的兜底）。窗口取 24h 是**保守上界**：实测下界是 8.6h，真实恢复点仍未知。

#### 注册被封时怎么继续干活

注册被封**不影响登录** —— 实测同一时刻：只读接口
（`cipher/getPubKey` / `personal/username/check` / `register/check`）全部 `success=true`，
已有账号登录 **6/6 成功**。所以封禁期不是只能干等：

```bash
# 用已有账号只测登录（不注册、不建 key，零注册配额消耗）
python tools/probes/probe_login_only.py --workers 6 --count 12 --offset 36
```

这也是本项目**测出浏览器侧并发天花板**的办法 —— 把注册这个混杂因素摘掉，
`workers` 的真实效果才第一次被单独观测到（见「workers 的边界」）。

#### 🔴 注册被封时怎么继续干活（二）：把**下游链路**整条测通

只测登录还是太保守 —— 注册之后的每一段其实都**与封禁无关**：
登录走 SSO、建 Key 走 `discovery.intern-ai.org.cn`、推理走
`discovery-api.intern-ai.org.cn`，是三个不同主机。

`tools/run_downstream.py` 从台账里取**已注册成功**的账号，只跑下游：

| 阶段 | 做什么 | 请求性质 |
|------|--------|---------|
| Stage 3 | 浏览器登录，取 JWT + Cookie | 写（SSO 登录） |
| Stage 4 | `getUserInfo` / `free-grant-status` / `balance` / `list_keys` | **纯只读** |
| Stage 5 | `ensure_key` 幂等建/复用 API Key | 写（仅建 Key 时） |
| Stage 6 | 真发一次 `chat/completions` | 写（消耗 ~0.0001 credits） |

**全程零注册请求**，所以不会加深 `B0000` 封禁，可以反复跑。

```bash
python tools/run_downstream.py                      # 4 个账号，只读 + 幂等复用
python tools/run_downstream.py --limit 12 --workers 4
python tools/run_downstream.py --create 1           # 其中 1 个真建新 key
python tools/run_downstream.py --no-write           # 只看结果，不落盘
```

实测结果（2026-09-18，12 账号 / 并发 4）：

```
下游全链路  12 账号 / 并发 4  wall=79.3s
  登录      12/12
  只读额度  12/12
  建/复用Key 12/12
  真实推理  11/12（1 个新建 key 传播未到位 → 见下）
```

**这是"账号真的能用"的唯一硬证据**：11 个账号的推理返回都是
`reply='成功'`，`usage.total_tokens` 97~118 —— 不是"接口 200"，是模型真的出了字。

三个设计点（每个都对应一次踩坑）：

- **`ensure_key` 命中已有 key 时返回 `key=""`**（列表接口不给明文）。
  复用分支**绝不能**用它去覆盖 `rec.api_key` —— 那会把一把好 key 洗成空串。
- **`--create` 默认关闭**。`create_key` 是**非幂等**的（每次新 `Idempotency-Key`，
  每次建一把新 key），不默认开启就不会每跑一次就在每个账号上堆一把 key。
- **新建的 key 在 Stage 6 必须先等传播**。第一次跑 `--create 1` 时，
  那个账号推理报 `401 Unauthorized` —— 而它 key 明明刚建成功。
  根因就是本文档「新建 key 有传播延迟」那节说的 ~10s。
  修复：Stage 6 对**本次新建**的 key 先 `wait_until_active`；
  复用路径不需要等（老 key 早就生效了）。

#### 🔴 查额度根本不用开浏览器：JWT 直查

`credits/balance` / `free-grant-status` / `getUserInfo` 这几个只读接口
**只认 JWT，不需要浏览器 Cookie**（见 [`docs/protocol.md`](docs/protocol.md)「JWT 有效期 14 天」一节）。
所以"查整个账号池的额度"这件事的代价是：

| 方式 | 单账号 | 15 个账号 |
|------|--------|----------|
| 浏览器登录后查 | 15~24s + 一次验证码 + 一个 Chrome 进程 | ~300s |
| **`tools/probes/probe_balance.py`（JWT 直查）** | **~0.03s** | **0.5s** |

```bash
python tools/probes/probe_balance.py              # 所有带 jwt 的账号，并发 8
python tools/probes/probe_balance.py --show-ok    # 连未消耗的也逐条列
```

这在本项目的处境下意义很大：**注册被封期间，台账里那批 09-15 签发、
有效期到 09-29 的 JWT 是唯一还能大规模利用的凭证**。拿它们就能把整个池子的
额度分布摸清楚，而不用碰浏览器、更不用碰注册。

实测（2026-09-18，15 个带 JWT 的账号）：`成功 15/15，JWT 过期 0，0.5s`。

> ⚠ 工具会**显式报出**"有多少条因为没有 JWT 查不了"（本次是 38/53）。
> 不报这个数会让人误以为"整个池子都查过了" —— 本项目对"静默缩水"有硬性戒心。

#### 补录历史（换机器 / 刚启用本功能时必做）

本地计数是**功能上线之后**才开始记的。此前已注册过的一批若不补录，计数会从 0
开始 —— 保护形同虚设，第一次跑必然"先撞墙再停"。补录命令（支持 `.json` 与
含它的 `.zip`）：

```bash
python -m src.quota                                  # 只看状态，不跑任何流程
python -m src.quota backfill ledger/runs/*/results-*.json   # 补录台账快照（重复项自动去重）
python -m src.quota backfill tmp/*.json backup.zip --dry-run   # 先看看会补多少
```

```
补录来源 12 个 → state: .workbuddy-ai/state/register_quota.jsonl
  新增 53 条，重复 0 条
  配额已用尽（53/40，窗口 24h），最早 196.0 分钟后可再注册
```

两个设计点：

- 判据是 `stages["register"] == "ok"`，**不是** `status == "success"` ——
  后者要求整链跑完，会漏掉"注册成功但登录失败"的账号，而它们**确实占了配额**。
  （本机实测：补录得到 **53** 条，与独立统计出的"53 个账号 / 53 个 Key"吻合。）
- 时间戳按记录里的 `created_at` **还原**，不是补录那一刻 —— 否则一批两小时前的
  记录会被误算成"刚刚发生"，窗口判断就废了。缺 `created_at` 的记录**不补录**
  （宁可少记，也不能给错时间）。

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

> 📄 **本节已迁至 [`docs/protocol.md`](docs/protocol.md)**（2026-09-20，B8）。
>
> 内容：密码加密 / 人机验证 / 浏览器反检测（最小化注入）/ 验证码两条通路 /
> 行为风控与轨迹 / Playwright 事件泵送陷阱 / discovery 鉴权与 JWT /
> 免费额度结构（双层滚动窗口 + 按 token 计费）/ key 创建与传播延迟 /
> 风控与频率 / CF Worker 临时邮箱 / 补录历史。
>
> 放在 `docs/` 而不是 README 的原因：README 面向「怎么用」，
> 而这一节是**实现知识**（与源码 docstring 同源）——集中一处才不会漂移。

## 实测耗时

### 单账号（顺序，`--workers 1`）

| 阶段 | 耗时 |
|------|------|
| 注册 | ~0.8 s |
| 激活（含收信） | ~6–9 s |
| 浏览器登录 | ~18–27 s |
| 领额度 + 建 Key | ~0.7 s |
| key 生效校验（末尾统一） | ~0.3 s |

### 下游链路单独跑（`tools/run_downstream.py`，2026-09-18）

跳过 Stage 1~2（注册 + 激活，也是被封的那两段），只跑 Stage 3~6：

| 阶段 | 单账号 | 说明 |
|------|--------|------|
| 浏览器登录（Stage 3） | **14.6~24.5 s** | 与整链时的登录耗时同量级 —— 登录就是慢 |
| 只读额度（Stage 4） | **0.42~1.46 s** | 4 个只读接口，中位 ~0.5 s |
| 建/复用 Key（Stage 5） | ~0.3 s | 复用路径；新建略高 |
| 真实推理（Stage 6） | **1.3~3.5 s** | 复用路径（老 key 已生效） |
| 真实推理（Stage 6，**新建 key**） | **6.4 s** | 多出的是 `wait_until_active` 等传播（~10s 上限内） |

12 账号 / 并发 4：`wall = 79.3 s`（其中登录占绝大部分）。

> **结论：下游链路的总耗时 ≈ 登录耗时**。Stage 4~6 加起来不到 5 s，
> 而登录一次 15~25 s。所以想加速下游，唯一的着力点是登录
> （或者干脆用已存 JWT 跳过登录 —— 只读接口不需要浏览器，见 `probe_balance.py`）。
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

**槽位代理池下的注册段（2026-09-18，`--count 4 --workers 2 --headless`）**

| 阶段 | 结果 |
|------|------|
| 建邮箱 | 4/4 |
| 注册（`register/byEmail`） | **4/4 成功**（`slot1`~`slot4`，4 个不同出口 IP） |
| 激活 | 0/4 —— 被邮箱 Worker 的 `Error 1101` 挡住（见 [`docs/protocol.md`](docs/protocol.md)「CF Worker 临时邮箱」） |
| 注册段耗时 | 单账号 3.7 ~ 7.3s，整批关键路径 7.5s |

> 这行的意义不在速度，在于**证明封禁真的被绕开了**：同一台机器、同一个本地
> 出口 `203.0.113.20`（已封 68h+），换成 4 个槽位出口后注册 4/4 全过。
> 注册段本身很快（闸门 1.2s/次，4 个账号并行 ≈ 7.5s），
> **瓶颈从来不是注册接口，是"你有几个能用的出口 IP"**。

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

### 台账落盘：`ledger/` 目录 + 日期 / 时间戳快照

`run.py --out` **不填时**（默认），台账落在仓库根的 `ledger/` 目录：

```
ledger/runs/2026-09-20/results-20260920-061230.json   ← 读源（**合并后的全量**）
ledger/runs/2026-09-20/results-20260920-055527.json   ← 上一份快照（回滚点）
ledger/latest.json                                    ← **本批结果**（含失败 / 跳过）
```

两个文件职责**不同**，混用是这块最容易踩的坑：

| 文件 | 内容 | 谁读它 |
|---|---|---|
| `runs/<日期>/results-<时间戳>.json` | **合并后的累计全量**（按 email 合并） | **所有工具** —— 它就是台账读源 |
| `ledger/latest.json` | **最近一次落盘写进去的记录**（跑批 = 本批含失败 / 跳过；整本重写 = 全量） | 只给人看「这次写了什么」 |

四条规则，缺一条都会把台账搞坏：

1. **读源是「最新的那份快照」，不是 `latest.json`。** 后者只含本批那几十条，
   拿它当读源 ⇒ 下一次运行合并的基准只剩上一批 ⇒ **台账停止累积、每次跑批
   覆盖上一次**（本项目栽过两次，第二次是 `--count 1` 把 53 条覆盖成 1 条）。
2. **快照里是「合并后的全量」，不是本次那几条。** 若只存本次结果，
   下一次运行读到的历史就只有上次那几条 ⇒ 台账被切碎 ⇒ 同上。
3. **`latest.json` 是内容副本，不是软链。** Windows 建软链要开发者模式；
   而「读不到台账」在本项目是最高危的静默失败（见「台账类用例」一节）。
   两个文件都用「写同目录临时文件 + 原子替换」刷新，读者永远看到完整的 JSON。
4. **写顺序是「先快照、后刷本批」。** 反过来的话，第二步失败就变成
   「读源已更新、快照没留下」—— 这次落盘没有回滚点。

`--out X` 显式给路径则是老行为：只写 `X`、**不落快照**（导出到别处用）。

取台账路径**一律**用 `ledger.ledger_path()`，别自己拼、也别拿 `latest.json`
顶替。读源每次落盘都换名字（时间戳），所以它是**算出来的**，不是常量；
把它冻进模块级常量，会让「读 → 跑 → 写回」的流程把结果写回**旧快照**。
整个 `ledger/` 目录在 `.gitignore` 里（里面是明文凭据），规则**按目录写**
而不是靠 `*.json` 通配兜底 —— 详见 `docs/security-conventions.md`「目录规范」。

每个账号一条记录（字段顺序就是 `AccountRecord.to_json()` 的顺序）：

```json
{
  "email": "oai-xxxxxxxx@<your-mail-domain>",
  "username": "lz123456",
  "password": "Lz#xxxxxxxxx",
  "sso_uid": "415100668",
  "jwt": "eyJ0eXBlIjoiSldUIi...",
  "api_key": "sk-...",
  "key_id": "ak_...",
  "credits": "10.000000",
  "status": "success",
  "error": "",
  "error_kind": "",
  "stages": {
    "register": "ok",
    "activate": "ok",
    "login": "ok",
    "key": "ok",
    "verify": "ok(10 models)"
  },
  "timings": { "register": 8200, "login": 17500, "key": 900 },
  "created_at": "2026-09-19 15:04:05",
  "proxy_slot": "slot3(http://127.0.0.1:17913)"
}
```

| 字段 | 什么时候有 | 说明 |
|------|-----------|------|
| `proxy_slot` | 槽位池模式 | 这个账号注册时用的出口槽位。**记它是为了事后能回答"被封的到底是哪个出口"** —— 光看 `B0000` 不知道维度。空 = 没启用槽位池 |
| `error` | 失败时 | 给人看的错误文本 |
| `error_kind` | 失败时 | 给代码判断的结构化类别，见下 |

> 注意 `verify` 只做**轻量校验**（能列模型即通过）—— 它**不证明能推理**。
> 要证明"真能用"必须真发一次 `chat/completions`，见
> `tools/ops/check_keys_alive.py`（它两级都做）。本项目吃过亏：
> 接口返回 200 + 一个 `sk-` 字符串，并不等于这个 key 能用。

### 🔴 `error_kind`：错误的**结构化类别** —— 不要再搜 `error` 文本

| 值 | 含义 | 判据来源 |
|---|---|---|
| `""` | 没有错误 | — |
| `"quota"` | 服务端返回 `B0000` —— **出口维度**累计配额触顶 | 服务端响应 |
| `"quota_guard"` | 本地守卫主动中止，**一个请求都没发** | 本地 |
| `"rejected"` | 服务端明确拒绝（非配额）：注册被拒 / 激活邮件没到 / `activate` 返回 false | 服务端响应 |
| `"network"` | 网络 / 超时 / HTTP 层 | 异常 |
| `"browser"` | 浏览器阶段（登录 / 建 key / worker 起不来） | 异常 |

**为什么必须有这个字段。** 在这之前，判断"这个失败是不是出口被封"靠
**在 `error` 文本里搜 `B0000`**。而**我们自己拼的守卫文案里也含 `B0000`**：

```
quota guard: 已确认 B0000（累计配额触顶），未发注册请求
```

文本匹配分不清"服务端返回的"和"我们引用的"。代价是实打实的 ——
`QuotaGovernor.note_result` 早就为此单独加了一句排除，注释写着
「不排除就会被当成新证据重复计数」；而 `settle_lease` 里同样的地雷还埋着：
一旦踩上，一个**一个请求都没发的干净出口**会被按长冷却晾 120s
（被封 120s vs 偶发故障 20s，差 6 倍）。

读点统一走 `pipeline.error_kind_of(rec)`：**字段非空即权威**，只有字段为空时
才退化成文本匹配（那是给老台账 / 手工构造的记录兜底的）。
**新增失败路径时必须在抛出点打标**，否则读点会静默退化成文本匹配。

`tests/test_error_kind.py` 钉住了三件事：每条路径打出的标、读点的权威性与兜底、
以及**新旧判据的分歧清单**（只有两条，且都不可达 —— 见
`tests/test_quota_governor.py::test_divergent_shapes_cannot_reach_settle_lease`）。

### 辅助产物（`.workbuddy-ai/exports/`，全部 gitignored）

| 文件 | 内容 |
|------|------|
| `keys_export.csv` / `.json` / `keys_only.txt` | 历史 key 导出（`tools/data/export_keys.py`） |
| `keys_alive.json` | 存活性报告（`tools/ops/check_keys_alive.py`）：`total/alive/dead/error` + 抽样推理结果 + `coverage`（台账/快照/未覆盖数，见下） |

**⚠ 前缀过滤会静默丢行**：`check_keys_alive.py` 只认 `api_key` 以 `sk-` 开头的行。
平台一旦改前缀（或 CSV 列名变了），它会**少测而不报错**，报告照样"全绿"。
现在会把丢掉的行数打出来：

```
⚠ 跳过 1/4 行：api_key 缺失或不以 'sk-' 开头
   （若这是意外，说明 CSV 列名或 key 前缀变了，别当成'没有死 key'）
```

**🔴 文件级防静默缩水（比上面那条高一层）**：`keys_export.csv` 是**某次快照**，
不随批次刷新。快照停在几天前时，行级过滤一切正常，但**连分母本身都是错的** ——
实测（2026-09-21）：快照 53 把 / 台账 605 把，跑出"53/53 存活"这种
**没测到却像全绿**的结论。

护栏落在**三处**，缺任何一处都会留下"artifact 看着全绿"的口子：

| 落点 | 内容 |
|------|------|
| stdout | `⚠ 导出快照**落后于台账**：台账 N 把带 key / 快照 M 把，本次结论**不覆盖**…` |
| artifact | `keys_alive.json` 的 `coverage` 块：`ledger_n` / `known_n` / `uncovered` / `covers_ledger` + 来源路径（**相对仓库根**，不带盘符） |
| 退出码 | 覆盖不足返回 `3`（与 `run.py` 的防静默缩水护栏同码）—— 只打印不改退出码的话，脚本化调用读到的是"成功" |

只想核验一个子集时用 `--allow-partial` 显式放行（退出码才回到 0）。
`tools/probes/probe_login_only.py` 是**同一道护栏的另一半**（比的是 `email` 不是
`api_key`），两边的 `coverage` 块与退出码保持一致。

**负对照验证**（2026-09-16，证明这个检查器不是"永远返回存活"）：

| 输入 | 期望 | 实测 |
|------|------|------|
| 真 key | alive | ✓ `model=deepseek-v4-flash-0731 text='成功'` |
| `sk-` + 40 个 `0` | dead | ✓ `HTTP 401 invalid API key` |
| `sk-abc`（截断） | dead | ✓ `HTTP 401` |
| 无 `sk-` 前缀 | 丢弃并告警 | ✓ 报"跳过 1/4 行" |

## ⚠️ 结果文件可能变成**唯一副本**

台账（`ledger/` 目录，含明文账号密码 + key + JWT）是 gitignored 的，
**不在仓库里**。而它很容易被当成"临时文件"清掉 —— 本项目就真发生过：

```
09-15  清理临时文件 → 打包备份到 _backups/Intern-Register-Tool-tmp-20260915.zip
09-16  _backups/ 整个目录被删 → 那 38 个账号的 key 只剩导出的 CSV 里有
```

**一旦台账和备份都没了，那批账号就永久失去访问凭据**（邮箱是临时邮箱，
收不到信；密码只存在于结果文件里）。key 本身还能用，但你再也查不到它的明文。

两道保险：

```bash
# 1. 定期导出（落 .workbuddy-ai/exports/，三重去重 + 目录 gitignore 校验）
python tools/data/export_keys.py

# 2. 导出文件本身也要另存到别处（网盘 / 加密盘 / 密码管理器）
#    工具会读自己上一次的 CSV 作为来源，所以重跑不会缩水（53 → 15 那种事不会再发生）
```

> 设计上的一条教训：**导出工具必须能读自己的输出**。
> 否则"来源被删"会让重跑**静默缩水**（53 把变 15 把），
> 而这种失败不会报错、不会提示，只会在某天你发现 key 少了一半时才暴露。

### 🔴 台账同时是"运行报告"和"账号台账" —— 合并规则必须只有一处实现

台账曾经是仓库根的一个 `results.json`，而它同时是 `run.py --out` 的默认目标。
后果是**一次小规模运行就能把台账覆盖掉**：

```
09-18  跑 `run.py --count 1` 探测服务端是否解封 → 53 条台账被覆盖成 1 条
```

（这已经是**第二次**同类事故 —— 第一次是 `_backups/` 被清理。）

2026-09-20 起台账搬进 `ledger/` 目录，每次落盘留一份日期 / 时间戳快照，
所以即使真被覆盖，`runs/` 里上一份快照还在。但这**没有放宽**下面这条规则 ——
合并保证的是**读源本身**永远不缩水，快照只兜住「还能捞回来」。

修复分三层，全部落在 `src/ledger.py`，**被所有会写台账的工具复用**
（`run.py` / `tools/run_downstream.py` / `tools/data/recover_activation.py` 用
`merge_records`；`tools/data/restore_results.py` 用 `merge_fragments`）：

1. **默认合并，不覆盖**。按 `email` 去重；老记录里本次没跑到的**保留**。
2. **失败不盖掉成功**。`status` 有优劣序（`success` 2 > `skipped` 1 > 其他 0），
   服务端抖一下不该把好账号标成坏。
3. **同级取并集**（`{**旧, **新}`）。这条是 2026-09-18 才补的 —— 补之前踩了个坑：

   > `tools/run_downstream.py` 交回的是**增量字段**（`jwt` / `credits` / `verify` /
   > 登录耗时），**没有 `status`** → `rank` 恒为 0。而旧记录要么 rank=2、
   > 要么 rank=0，于是 `rank(新) > rank(旧)` **永远为假**：
   > 下游跑了半天，字段一个都没写进台账，而打印出来的一切都"正常"。
   >
   > 这正是本项目最警惕的一类失败 —— **数据静默缩水，指标全绿**。
   >
   > 也试过"比非空字段个数"，同样是启发式：两边字段数**相等**时照样丢字段
   > （自测 `T9` 当场抓到）。**并集没有这个漏洞** —— 它不靠猜，
   > 数学上保证字段只增不减。

4. **防静默缩水护栏**：`ledger.save()` / `ledger.save_snapshot()` 发现
   "合并后条数 < 原有条数"直接抛异常、退出码 3，宁可报错也不静默丢账号。
   要显式覆盖得用 `run.py --overwrite`。

### 重建台账时的合并规则**不同**（`merge_fragments`）

`tools/data/restore_results.py` 从散落来源重建台账，用的是 `ledger.merge_fragments()`，
而不是 `merge_records()`。看着像重复，其实**降级行为必须不同**：

| | `merge_records`（运行期） | `merge_fragments`（重建） |
|---|---|---|
| 降级记录是什么 | **一次失败尝试**（带 `error` / 中间态） | **`export_keys` 的导出行**（带 `source` / `verify`） |
| 降级时 | **不动** | **补缺口** |

2026-09-19 实测：把 `restore_results` 改成调用 `merge_records`，
**15 个账号丢 18 个字段**（`source` × 15、`verify` × 3），而这 15 个账号本来
都够得着理论最大字段集。完整推导见 `src/ledger.py` 的 `merge_fragments` docstring。

规则本身由 `tests/test_ledger_merge.py` + `tests/test_ledger_fragments.py`
离线钉住（**零网络请求**）：

```bash
python -m pytest tests/test_ledger_merge.py tests/test_ledger_fragments.py
```

台账已经丢过两次，所以这里宁可多写测试。两个文件覆盖：
不丢历史 / 失败不盖成功 / 成功覆盖失败 / 同 email 去重 / 无 email 保留 /
损坏文件不崩 / 护栏（变少→抛且文件未改动）/ 并集合并 / rank 优先于字段数 /
碎片合并（导出行补缺口、键取并集、与 `merge_records` 的对照）。

### 🔴 台账类用例的输入从哪来 —— `any_ledger`（样本 + 真实）

这些用例需要"一本真实形状的台账"当基线，而基线**不能硬编码条数**
（台账是活的：每跑一次批量注册就变多，写死条数的话下次正常注册就会被判
"测试失败" —— 那是测试在撒谎，不是代码坏了）。

但真实台账（`ledger/runs/<日期>/results-<时间戳>.json`，读源 = 最新那份）
含凭据、**不在仓库里** ⇒ CI 上读到的是 `[]`。
**空输入是最坏的一种降级**：它不报错，而是让每个用例以各自的形态给出
无意义的结论 —— 2026-09-19 CI 第二次变红时，同一个根因炸出了四种形态：

| 用例 | 空基线下的表现 |
|---|---|
| `test_t1` / `test_t8` | `len([]) == 0 + 1` 成立 → **碰巧通过（假绿）** |
| `test_t2` | `next()` 找不到 `status=success` → 裸 `StopIteration` |
| `test_t7` | `save(p, [])` vs `[]` 不算缩水 → `DID NOT RAISE ValueError` |
| `test_ledger_fragments` 两条 | `assert real` → `AssertionError` |

所以现在由 `any_ledger` 夹具统一裁决，**两个来源都跑**：

- `ledger_sample` —— `tests/fixtures/ledger_sample.json`，形状复刻真实台账、
  值全编造，**任何环境都可用**（CI 靠它跑）；
- `real_ledger` —— 本地真实台账，读不到就**大声跳过**（`-rs` 会把原因打进日志），
  绝不返回 `[]`。

⚠ 刻意不做成"有真数据就用真数据、没有就用样本"：那样本地测的输入和 CI 测的
输入**不是同一个**，本地绿就证明不了 CI 绿 —— 而那正是这批失败的根本形态。

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
> 在最小化注入 + 人类轨迹的实现下实测三组无头配置（`tools/probes/probe_headless.py`）：

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

### 2026-09-20 起：**默认就是无头**

```bash
python run.py                 # 默认无头，不弹窗口
python run.py --headful       # 要弹窗口时显式指定
```

翻默认值的理由：当天实测**漏写 `--headless` 跑了一整批有头**。
根因不是代码 bug，而是**默认值本身是有头** —— 忘了写就弹窗口，而且不报错。
默认翻过来之后，忘了写也不会误弹窗口。

> ⚠ `--headless` 参数**保留**（现在是幂等的 no-op），因为旧脚本与文档里到处是它；
> 删掉会让那些命令直接报错。
>
> ⚠ 别再宣称"无头更快"：2026-09-20 在 `workers=4` 量级上实测，
> **无头与有头的吞吐没有可测差异**（两次无头 50 批次关键路径 194.2s / 196.5s；
> 有头 100 批次 370.6s = 3.7s/账号 —— 但 50 与 100 不可直接比，
> 50 = 4×12+2 有 worker 空转的尾巴）。
> 无头的真正好处是**不弹窗口**，不是速度。

## 已知限制

- 验证码若切到滑块模式（`captchaType` 非 `CHECK_BOX`）需另加滑动轨迹模拟，
  当前实现只检测并上报（`captcha_stage.slider`），不做自动破解
- **`workers` 已实测到 6 都安全**（只测登录阶段：12 账号 / 6 路 = 2 轮，
  12/12 成功、零 `F001`、3.6s/账号）。默认取 4。
  ⚠ 但**整链**在 `workers=6` 下尚未复测 —— 注册被 IP 封着，跑不了
- 登录页**没有"直达密码表单"的路由**（SPA 用内部状态切 tab，URL 不变，已实测）。
  `form_ready` 的水合时间无法通过改 URL 省掉
- 免费额度为 `pkg_free_monthly_base`，10 credits / 5 小时窗口，rpm 50
- 单账号登录仍需 ~15–27s，其中 `submit → Init#1` 的 5~7s 是验证码 SDK
  内部酝酿时间，**无法压缩**
- **注册阶段已到硬下限**：激活邮件真正到达就要 **6.02s**（SMTP + Worker 入库，
  实测拆解见 [`docs/protocol.md`](docs/protocol.md)「CF Worker 临时邮箱」），加上建邮箱/查重/注册/激活，
  单账号注册最快 ~8.3s，没有进一步压缩空间
- **登录耗时方差大**（实测极差可达 36.4s，出现过一个 51.2s 的离群值）。
  批量总时长由**最慢那个账号**决定，不是均值 —— 所以报告里打印的是最慢账号的
  分解，不是第一个
- **注册封禁是 IP 维度、窗口 > 68.2h、恢复点未知**。实测：最后成功注册
  `09-15 14:05:43` → `09-16 09:16`（**19.2 小时后**）仍 `B0000`，
  → `09-18 10:19`（**68.2 小时后、跨 3 个自然日**）仍是 `B0000`。
  换发信域名无效（已实测）；**也不是"每天 0 点重置"**（否则 09-16 凌晨就该恢复，
  且 68.2h 已跨 3 个零点）。只能等窗口或换出口 IP
- **🔀 换出口 IP 已实测解开封禁**（2026-09-18）：3 个不同出口各打一枪 →
  **3/3 注册成功**；槽位池跑真实批量 → **注册 4/4 成功**。
  所以"本机被封"不再是死局，但**能注册多少取决于有多少个不同出口 IP**
  （实测 6 个槽位只对应 4 个出口），不是本地并发度
- **🔴 邮箱 Worker 的 `/admin/all` 从 2026-09-18 起间歇性抛 Cloudflare
  `Error 1101`**，根因是 **D1 读取超限额**（维护者已确认）——
  无 WHERE 的全表 `SELECT *`（含 1.5MB 上限的大字段）**每请求一次就是一次全表读**。
  当天下午"修复"后复核：成功率从 4%（1/25）升到 **20%（1/5）**，
  **仍不可用**；且**与 `limit` 大小无关**（`limit=1` 和 `limit=50` 一样 500），
  带 `email` 过滤那条路径**一律 500**。
  → **在 Worker 真正修好前不要跑批量注册** —— 每次注册都在花全站共享的 D1 读取，
  打满之后连你自己也读不出来。详见 [`docs/protocol.md`](docs/protocol.md)「CF Worker 临时邮箱」一节。
  已注册未激活的账号可用 `tools/data/recover_activation.py` 补激活（**同样要等 Worker 恢复**）
- **整条链路真正的约束是"每账号每周 50 credits"**（见 [`docs/protocol.md`](docs/protocol.md)「免费额度的真实结构」），
  不是本地的任何并发参数。53 个账号 ≈ 2,650 credits/周。
  **要规模化只能靠更多账号（本机被封时 = 更多出口 IP）**
- **封禁期登录不受影响** —— 只读接口与已有账号登录都正常（实测 12/12 登录成功），
  且 JWT 有 14 天有效期，查余额/额度完全不需要浏览器
- **Stage 3~6（下游全链路）已单独验证可跑通**，零注册请求：
  12 账号实测 登录 12/12、只读 12/12、建/复用 Key 12/12、真实推理 11/12
  （唯一失败是新建 key 的 ~10s 传播延迟，已修）。见 `tools/run_downstream.py`
- **`available_credits` 是误导性指标**：它 = `5h限额 − 7d已用`（15/15 实测命中），
  5h 窗口没动也可能显示 < 10。判额度必须看 `usage_windows[*].used_credits`
- **账号池里已有 3 个账号的 7d 额度被外部消耗**（0.43 / 0.98 / 1.98 credits，
  2026-09-18 实测）。本项目自己的测试调用一次约 0.0001 credits，**量级差 4 个数量级**
  → 消耗不是本项目产生的。导出过的 key 在别处被真实使用过。
  用 `tools/probes/probe_balance.py` 可复查消耗增速
- **`ok=True` 不等于"有正文"**：默认模型带 reasoning，`content` 可能因
  `max_tokens` 被推理占满而为空（见 [`docs/protocol.md`](docs/protocol.md)「`max_tokens` 给太小会把好 key 报成坏的」）。
  下游判"成功"要一起看 `finish_reason`，别只看 HTTP 200
- **53 把 key 的存活性只代表"当下"**：平台随时可能回收额度或封 key。
  `tools/ops/check_keys_alive.py` 是可重复的复查手段（含负对照验证），
  但它读的是导出 CSV —— 导出文件本身要是丢了，就无从复查（见上一节）

---

## 免责声明

本项目是**协议逆向与浏览器自动化的技术研究**，目的在于搞清 HTTP 链路与前端风控
（阿里云验证码 2.0）的交互机制。

- 请勿用于批量刷取、账号转售或任何违反目标平台服务条款的用途
- 工具**不绕过任何付费环节**，也不篡改额度 —— 领的就是平台公开提供的免费额度
- 使用产生的任何后果由使用者自行承担

仓库内**不含任何真实账号数据**。`ledger/`（含明文账号/密码/JWT/API Key）、
`.env`（含 Admin Token）、`.workbuddy-ai/`（本地开发记录）均已在 `.gitignore` 中排除。

唯一的例外是 `tests/fixtures/ledger_sample.json` —— 一份**脱敏样本台账**，
16 条记录、字段形状逐档复刻真实台账（14 / 33 / 34 键的成功记录、
空 email 的配额拦截记录、9 键的 `export_keys` 导出行、`0` / `False` / `None` /
`""` / `{}` / `[]` / 非 ASCII 值），但**值全是编造的**，email 只用 RFC 2606
保留域 `example.com`。它存在的原因是：真实台账含凭据、不入库 ⇒ CI 上读不到 ⇒
依赖"一本真实形状的台账"的回归用例在 CI 上全部失效（2026-09-19 CI 第二次变红）。
样本本身由 `tests/test_ledger_sample.py` 逐条钉住，包括
「不许出现真凭据形态的串」「email 必须落在保留域」「形状覆盖不许缩水」。

## 许可

MIT
