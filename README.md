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


### 🔴 限流 / 配额 / 吞吐的实测细节 → [`docs/protocol.md`](docs/protocol.md)

上面两条是**结构**；它们背后的**实测结论**已迁到 protocol.md（README 只留
「怎么用 + 架构 + 已知限制」，见 [`docs/README.md`](docs/README.md) 的约定）：

- `429` 的真实边界挂在**写操作 + 突发**上（不是并发度）—— `REG_MIN_INTERVAL` 的依据；
- `workers` 的边界：浏览器侧实测到 12 都零失败，**真正的墙是注册配额**；
- 注册配额是**累计量**限制（`B0000`，IP 维度、窗口 > 68.2h）—— 它才是规模化的约束；
- WAF 解盾的成本与按出口复用、重试预算 `POST_ATTEMPTS` 的取值依据。

> 改 `workers` / `REG_MIN_INTERVAL` / 配额相关代码前，先读 protocol.md 的
> 「注册链路：限流、配额与吞吐（实测）」一节。

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
> 风控与频率 / CF Worker 临时邮箱 / 补录历史 /
> **注册链路限流·配额·吞吐实测** / **实测耗时**（单账号·下游链·批量）。
>
> 放在 `docs/` 而不是 README 的原因：README 面向「怎么用」，
> 而这一节是**实现知识**（与源码 docstring 同源）——集中一处才不会漂移。



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
- **注册阶段的耗时大头是 WAF 解盾，不是 SMTP**：激活邮件真正到达要 **6.02s**
  （SMTP + Worker 入库，实测拆解见 [`docs/protocol.md`](docs/protocol.md)「CF Worker 临时邮箱」），
  但 10-03 ~ 10-05 实测 `register_call` 中位 **14.7s**、**92%** 落在 >10s ——
  多出来的十几秒是**每个账号解一次 WAF 挑战**（启一个真实 Chrome）。
  2026-10-06 起按出口缓存 `acw_sc__v2`（`WafCookieCache` / `_SsoPool`），
  同一出口的下一个账号直接复用；效果由台账里的 `waf_solve_ms` 复核。
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
