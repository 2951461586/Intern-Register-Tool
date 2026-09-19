# #14 执行方案：拆 `src/browser_login.py`

> 对应 `docs/refactor-plan-2026-09-19.md` §6 阶段三 #14。
> 本文所有数字均由 `.workbuddy-ai/tmp/analyze_browser_login.py` 复算得出，
> 遵守 [`security-conventions.md`](security-conventions.md) 的占位符约定。

---

## 0. 结论（先看这段）

**建议：先做「阶段 A 文件内重构」，暂缓「阶段 B 拆包」。**

| | 阶段 A 文件内重构 | 阶段 B 拆 `src/browser/` |
|---|---|---|
| 做什么 | 把 `_run_attempt` 的状态容器化、8 个步骤抽函数、消掉重试循环重复 | 把 831 行拆成 4 个文件 + `__init__.py` |
| 公共 API | **零变化** | 需要 9 处引用同步改，或留兼容壳 |
| 风险 | **中**（改行为结构，不改位置） | **高**（见 §2 的三条硬风险） |
| `_run_attempt` 行数 | 268 → **~50** | 268 → ~50（同 A，B 不额外降） |
| 收益 | 可读性、消重复 | 仅"文件数变多" |

**核心判断：方案 §3.1 说"拆完 `_run_attempt` 应该降到 60~80 行"—— 这个数字靠拆文件拿不到，靠阶段 A 才拿得到。**
拆包（B）本身不减少任何函数长度，它只是把长函数换个文件放。

而 B 的代价是三条硬风险（§2），其中一条会**静默失效**。所以顺序应该是 **A 先做、B 缓做**；
若最终要做 B，必须先解决 §2.1 那条。

---

## 1. 实测画像（可复算）

跑 `.workbuddy-ai/tmp/analyze_browser_login.py` 得到：

### 1.1 文件构成 —— 注释确实是资产，但不是长度的主因

| 类别 | 行数 | 占比 |
|---|---|---|
| code | 394 | 47.4% |
| comment（`#`） | 147 | 17.7% |
| docstring | 193 | 23.2% |
| blank | 97 | 11.7% |
| **合计** | **831** | |

注释 + docstring = **40.9%**。其中模块 docstring 独占 **L1–144（144 行）**。

> ⚠ 但这不能推出"函数长是因为注释多"。见下。

### 1.2 各顶层定义

| 名称 | 行区间 | 总行 | 注释行 | 注释% |
|---|---|---|---|---|
| `LoginResult` | 266–274 | 9 | 0 | 0.0% |
| `_human_move` | 280–310 | 31 | 1 | 3.2% |
| `_idle_wait` | 313–326 | 14 | 0 | 0.0% |
| `_micro_move` | 329–356 | 28 | 0 | 0.0% |
| `_warmup_mouse` | 359–371 | 13 | 0 | 0.0% |
| `_click_checkbox` | 374–397 | 24 | 1 | 4.2% |
| `_has_slider` | 400–418 | 19 | 0 | 0.0% |
| `build_login_url` | 421–426 | 6 | 0 | 0.0% |
| **`_run_attempt`** | **432–699** | **268** | **36** | **13.4%** |
| `BrowserSession` | 705–773 | 69 | 2 | 2.9% |
| `login` | 779–831 | 53 | 1 | 1.9% |

**🔴 关键数字：`_run_attempt` 268 行里注释只占 36 行（13.4%），代码约 232 行。**
它不是"注释撑起来的"，是真的长。**所以"把注释搬走就能瘦"这条路不存在。**

（方案 §3.1 写的是 432–705 / 274 行，含尾部空行与分隔线；AST 精确边界是 432–699 / 268 行。）

### 1.3 `_run_attempt` 内部：3 个闭包 + 5 个共享状态

闭包捕获（AST 实测）：

| 闭包 | 行 | 捕获的外层名 |
|---|---|---|
| `mark` | 443–444 | `t_start`, `tm` |
| `ev` | 446–453 | `cap`, `t_start` |
| **`on_response`** | **477–519** | **`cap`, `code_holder`, `ev`, `jwt_holder`, `verbose`** |

顶部状态容器：

```
jwt_holder = {"jwt": ""}        # on_response 写、主流程读
code_holder = {"code": ""}      # on_response 写、结果构造读
cap = {"init": 0, "verify": [], "last_ok": False, "payload": "",
       "slider": "", "trivial": 0, "events": []}   # 双方都读写
mv = {}                         # 鼠标统计，传给 _warmup_mouse/_micro_move/_idle_wait
tm = {}                         # mark() 写
t_start = time.time()
```

**这就是 `_run_attempt` 不能简单切分的根因**：`on_response` 是 Playwright 的
事件回调，它与主流程**通过 5 个共享 dict 通信**，而主流程的循环
（L595 / L617 / L631）又在多个 `break` 点读 `jwt_holder["jwt"]`。
想把这些切成独立函数，就得把 dict 传进传出 —— **接口会比实现还复杂**。

### 1.4 流程的 8 个天然切点

注释里的编号 + `mark()` 埋点一一对应：

| 步骤 | 行 | mark 锚点 |
|---|---|---|
| （导航） | 524–525 | `goto` |
| （预加载实验） | 529–531 | `prewarm` |
| 1) 切到账号登录 | 533–538 | — |
| 2) 切密码 tab + 等输入框 | 540–548 | `form_ready` |
| 3) 逐字输入 | 550–560 | `typed` |
| 4) 勾选协议 | 562–570 | `checkbox` |
| 5) 短暖场 | 572–575 | `warmup` |
| 6) 提交 | 580–582 | — |
| 7) 等验证码结果 | 584–614 | `captcha_ready` |
| 8) 点击重试循环 | 616–648 | `done` |
| （结果构造） | 650–683 | — |
| （异常/清理） | 684–699 | — |

**8 个 mark 锚点就是天然的切分边界** —— 这不是我发明的，是原作者埋好的。

### 1.5 顺带发现：两处重试循环重复约 20 行

`login()`（L801–831）与 `BrowserSession.login()`（L752–773）是**两份几乎相同的重试循环**：

| 相同 | 不同 |
|---|---|
| `last = LoginResult(ok=False, ...)` | 一个 `with sync_playwright()` 每次新建浏览器 |
| `for i in range(max(1, attempts))` | 一个复用 `self._browser` |
| `tag = f"_a{i + 1}"` | |
| verbose 打印「=== 尝试 i/n ===」 | |
| `res.attempts_used = i + 1` | |
| `if res.ok: return res` / `last = res` | |
| `wait = cooldown * (i + 1) + random.uniform(0, 5)` | |
| verbose 打印冷却秒数 | |

这是**真实的、可消的重复**，而且不需要碰任何浏览器逻辑。阶段 A 顺手做掉。

---

## 2. 三条硬风险（按严重度）

### 2.1 🔴 P0：`probe_headless.py` 运行时改写模块级常量 —— 拆包会让它**静默失效**

`tools/probes/probe_headless.py:74-88`：

```python
import src.browser_login as bl

orig = bl.CHROME_ARGS
bl.CHROME_ARGS = BASE_ARGS + extra_args      # ← 改写模块全局
try:
    res = bl.login(..., headless=True, ...)
finally:
    bl.CHROME_ARGS = orig
```

它靠的是 **Python 语义：模块属性赋值 == 改该模块的全局命名空间**，
所以同模块内的 `launch(args=CHROME_ARGS)`（L734 / L808）会读到新值。

**拆包后这个 patch 会失效**：

- 若 `src/browser_login.py` 变成 re-export 壳（`from .browser.session import CHROME_ARGS`），
  `bl.CHROME_ARGS = X` 只改**壳模块的属性**，
  真正的 `src.browser.session.CHROME_ARGS` **一点没变**。
- 最坏的地方是**它不报错**：探针照常跑完、照常打印结果，
  但用的是默认 args —— 实验结论失真，而人会以为探针有效。

这正是 `python-compat-shell-refactor` skill 记的那类坑
（"patch 壳上的符号静默失效"），本项目已在别处踩过一次。

**必须先修**（无论最终拆不拆，这条都值得修）：

| 方案 | 做法 | 评价 |
|---|---|---|
| ① 改 patch 目标 | 探针改 `patch src.browser.session.CHROME_ARGS`（改真源） | 简单，但把"模块内部结构"暴露给探针 |
| ② **改成可注入参数** | `BrowserSession(headless=..., chrome_args=None)` / `login(..., chrome_args=None)`，默认 `None` → 用 `CHROME_ARGS` | **推荐**：探针不再依赖模块内部布局，拆包自由 |
| ③ 拆包时不留壳，改 9 处引用 | 探针改 `from src.browser import login` | 可行但探针仍有 ② 的问题 |

### 2.2 🔴 P1：这个模块**零测试覆盖**

`grep -rln "browser_login" tests/` → **无**。

也就是说：**拆它没有回归网**。`captcha_stage` 的键、`timings` 的键、
`LoginResult` 的字段被下游（台账 `results.json`、探针、`run_downstream`）依赖，
改坏了不会有测试红，只会在某次跑批时表现为"字段没了"。

**这是"高风险"评价的真正来源**，比"注释是资产"更硬 ——
注释丢了能看出来，字段丢了看不出来。

### 2.3 P1：注释必须跟着代码走（方案已指出，这里补充量化）

方案 §3.1 已说"145 行头注释必须跟代码走"。补充两点：

- 精确数字是 **L1–144 共 144 行**（模块 docstring）。
- 它记录的是**跨函数**的结论（最小化注入、F001 归因、两条通路不能混比），
  不属于任何一个函数。**拆包后它没有天然的归属地** ——
  放 `__init__.py` 会被当成"包的介绍"，放 `login.py` 会被当成"函数的说明"，
  两种都是错的。建议拆包时**原样留在包的 `__init__.py` 顶部**并加一行
  `# 本节结论横跨本包全部模块，改动前先读完`。

---

## 3. 阶段 A：文件内重构 ✅ **已完成（2026-09-19 晚）**

> 落地记录见文末 §9。`_run_attempt` **268 → 70 行**，闭包捕获 **3 → 0**。

**目标**：`_run_attempt` 268 → ~50 行；`login` 53 → ~25 行；**公共 API 零变化**。

### A1. 抽 `_AttemptState`（消掉 5 个共享 dict）

```python
class _AttemptState:
    """一次登录尝试的全部可变状态 + 埋点。

    为什么要容器化：`on_response` 是 Playwright 的事件回调，与主流程
    通过 5 个 dict 通信。抽成类之后，回调与步骤函数都只吃一个 state。
    """
    def __init__(self):
        self.t_start = time.time()
        self.jwt = ""              # 原 jwt_holder["jwt"]
        self.code = ""             # 原 code_holder["code"]
        self.cap = {...}           # 形状逐字不变
        self.mv = {}
        self.timings = {}

    def mark(self, name: str) -> None: ...
    def ev(self, kind: str, detail: str = "") -> None: ...
    def on_response(self, resp) -> None: ...   # 原闭包 → 方法
```

**硬约束：`self.cap` / `self.mv` 的键名与形状逐字不变** ——
它们最终被展开进 `LoginResult.captcha_stage`（L666–681），
下游依赖那些键。

### A2. 8 个步骤抽成模块级私有函数

按 §1.4 的 8 个 mark 锚点切，签名统一：

```python
def _step_open_form(page, st: _AttemptState) -> None: ...          # 步骤 1+2
def _step_type_credentials(page, st, account, password) -> None: ...  # 步骤 3
def _step_accept_agreements(page, st) -> None: ...                 # 步骤 4
def _step_warmup(page, st, vw, vh, cur) -> tuple: ...              # 步骤 5
def _step_submit(page, st) -> None: ...                            # 步骤 6
def _step_wait_captcha(page, st, *, timeout, vw, vh, cur, verbose) -> tuple: ...  # 步骤 7
def _step_click_until_jwt(page, st, *, vw, vh, cur, verbose) -> tuple: ...        # 步骤 8
def _build_result(st, *, cookies, jwt, waited) -> LoginResult: ...  # 结果构造
```

⚠ `_step_wait_captcha` / `_step_click_until_jwt` 的签名偏长（吃 `cur` / `vw` / `vh`），
这是**真实的耦合**，不要为了"签名好看"把它们塞进 state —— `vw`/`vh`/`cur` 是
页面维度与鼠标位置，不是"尝试的状态"。

### A3. 抽 `_retry_loop`（消掉 §1.5 的 20 行重复）

```python
def _retry_loop(make_browser, *, attempts, cooldown, headless,
                timeout, screenshot_prefix, verbose) -> LoginResult:
    """两次调用者共用的重试循环。

    `make_browser` 是上下文管理器：单账号版每次新建浏览器，
    会话版直接复用 self._browser（其 __exit__ 是空操作）。
    """
```

### A4. 阶段 A 的验证判据

| 判据 | 怎么验 |
|---|---|
| `captcha_stage` 键集合逐字不变 | 新写 `tests/test_browser_login.py`，断言键集合 == 冻结清单 |
| `LoginResult` 字段不变 | 同上，断言 `dataclasses.fields()` |
| `build_login_url()` 输出不变 | 直接断言字符串（这个不需要浏览器，现在就能测） |
| 重试循环的计数/冷却逻辑等价 | 注入假 `make_browser`（返回假 `LoginResult`），断言 `attempts_used` 与 `time.sleep` 调用序列 |
| 公共 API 未变 | `probe_*` 的 `--help` 与导入链冒烟（30/30） |
| 真浏览器路径 | **无法离线验证** —— 见 §5 |

**A4 顺带解决 §2.2**：这四条判据里前四条都不需要真浏览器，
可以在阶段 A 就补上，把"零测试"补成"关键契约有测试"。

---

## 4. 阶段 B：拆包 ✅ **已完成（2026-09-19 晚）—— 见 §10**

> 下面 §4.1~§4.3 是**执行前的方案**，保留原样作为决策记录。
> 实际落地与方案的差异（新增 2 个叶子模块、`build_login_url` 的循环导入问题、
> 漏掉的第 10 处引用面）全部记在 §10.2 与 §10.5。

### B0. 前置（必须先做，否则 B 会引入静默失效）

1. **解决 `CHROME_ARGS` 注入**（§2.1 方案 ②）：改成参数，默认 `None` → 用模块常量。
2. **补测试**（§2.2 / A4）：至少把 `captcha_stage` 键集合与重试循环钉住。
3. 跑一遍全量验收留基线。

### B1. 目标结构（在方案 §3.1 基础上微调）

```
src/browser/
    __init__.py      144 行模块 docstring（原样搬）+ 公共 API re-export
    state.py         _AttemptState（阶段 A 产物）
    behavior.py      _human_move / _idle_wait / _micro_move / _warmup_mouse
    captcha.py       _click_checkbox / _has_slider / 通路判定
    attempt.py       _run_attempt 编排 + 8 个 _step_*
    session.py       BrowserSession + _retry_loop
    entry.py         login()（单账号入口）+ build_login_url()
```

**与方案 §3.1 的差异**：方案只列了 4 个文件，实测后建议 **7 个** ——
因为 `behavior.py`（5 函数 / 115 行）与 `captcha.py`（2 函数 / 43 行）
职责确实不同，且 `state.py` 是阶段 A 引入的新东西，需要一个家。

### B2. 引用面（9 处，必须同步改）

**生产代码（3 处）**：
| 位置 | 用法 |
|---|---|
| `src/pipeline.py:564-566` | `from .browser_login import login as browser_login` |
| `src/pipeline.py:772` | `from .browser_login import BrowserSession` |
| `tools/run_downstream.py:107` | `from src.browser_login import BrowserSession` |

**探针（6 处 / 5 个文件）**：
| 位置 | 用法 | 迁移难点 |
|---|---|---|
| `probes/probe_captcha_timing.py:36` | `from src import browser_login` → `.MICRO_MOVE` / `.MICRO_BUDGET_S` / `.login` | 读模块常量 |
| `probes/probe_headless.py:75` | `import src.browser_login as bl` → **patch `bl.CHROME_ARGS`** | **§2.1，必须先修** |
| `probes/probe_login_only.py:112` | `from src.browser_login import BrowserSession` | 直接 |
| `probes/probe_login_route.py:29` | `from src.browser_login import CHROME_ARGS, build_login_url` | 读模块常量 |
| `probes/probe_login_timing.py:45` | `from src import browser_login` → `.PREWARM_MS` / `.TYPE_DELAY_LO/HI` / `.login` | 读模块常量 |

**三条路，选一**：

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **① 不留壳，改 9 处** | 全部改成 `from src.browser import ...` | 最干净，无隐性耦合 | 探针的常量读取要改成从 `src.browser` 读；改动面最大 |
| **② 留薄壳** | `src/browser_login.py` 只做 re-export | 改动面最小（0 处） | **`CHROME_ARGS` patch 静默失效**（除非 B0 已修）；违反"单一真源" |
| **③ 留壳 + 壳内转发** | 壳里 `CHROME_ARGS` 用 `__getattr__` 转发到真源 | 兼顾 | 复杂度上升，且 `__getattr__` 对 patch 仍无效 |

**建议 ①**（配合 B0 的 ② 号修法）：探针读的常量全部从 `src.browser` 读，
`CHROME_ARGS` 改成参数后探针不再需要 patch。

### B3. 阶段 B 的验证判据

| 判据 | 怎么验 |
|---|---|
| 导入链 30/30 | `.workbuddy-ai/tmp/import_smoke.py` |
| `--help` 20/20 | `.workbuddy-ai/tmp/help_smoke.py` |
| 生产路径能 import | `python -c "from src.browser import login, BrowserSession"` |
| **`CHROME_ARGS` 注入生效** | 新写探针自检：注入自定义 args → 断言 `launch()` 收到的是它 |
| README / docs 路径引用 | 正则 → `exists()` |
| 真浏览器路径 | **无法离线验证** —— 见 §5 |

---

## 5. ⚠ 无法离线验证的部分（必须说清楚）

`_run_attempt` 的**全部实质行为都在真浏览器里**：验证码通路、点击轨迹、
JWT 捕获。本机当前：

- 注册被 IP 维度封禁（`B0000`），批量跑不了；
- 但**登录不受影响** —— 实测封禁期已有账号登录 6/6 成功，
  且 `tools/probes/probe_login_only.py` 就是干这个的。

所以阶段 A / B 的**最终验收必须跑一次真登录**：

```bash
# 用已有账号，只测登录，零注册请求
python tools/probes/probe_login_only.py --help    # 先看参数
python tools/run_downstream.py --help              # 或走下游全链路
```

**在跑通这个之前，任何"拆分成功"的说法都只是静态检查通过**，
不能说行为等价。

---

## 6. 回滚

| 阶段 | 回滚方式 |
|---|---|
| A | 单文件改动，`git checkout -- src/browser_login.py`；新增的 `tests/test_browser_login.py` 可保留 |
| B | 拆包前把 `src/browser_login.py` 复制到 `.workbuddy-ai/tmp/quarantine/`；回滚 = 把目录移回 + 还原 9 处引用（`git checkout` 即可，因为引用改动都在 git 里） |

---

## 7. 明确不建议做的事

1. **不要为了"降到 60 行"而把 `on_response` 塞进参数传递。**
   它捕获 5 个状态，硬传参会让签名变成 7 个参数 —— 接口比实现复杂，
   这是"为了指标而重构"。
2. **不要在没修 `CHROME_ARGS` 注入之前拆包。** 会让 `probe_headless.py` 静默失效。
3. **不要同时做 A 和 B。** 一个是行为改造，一个是文件搬迁。
   混在一起时测试红了分不清是谁的锅 —— 这正是本项目 `python-module-split-refactor`
   skill 的第一条纪律（"不要把手拆和重构混在一起"）。
4. **不要把 144 行模块 docstring 拆散。** 它记的是跨函数结论，
   拆散后每段都失去了上下文。

---

## 8. 建议的执行顺序（一页版）

```
[ ] 1. 修 CHROME_ARGS 注入方式（改成参数，默认 None）    ← 独立价值，先做
[ ] 2. 补 tests/test_browser_login.py（契约 4 条，不需浏览器）
[ ] 3. 阶段 A：_AttemptState + 8 个 _step_* + _retry_loop
[ ] 4. 跑 pytest / ruff / 导入链 / --help，留基线
[ ] 5. 跑一次真登录（probe_login_only.py 或 run_downstream.py）← 唯一的行为验收
[ ] 6. （可选）阶段 B：拆包 + 改 9 处引用
[ ] 7. （可选）再跑一次真登录
```

**第 5 步没跑通之前，第 3 步不算完成。**

---

## 9. 阶段 A 落地记录（2026-09-19 晚）

### 9.1 做了什么

| # | 动作 | 结果 |
|---|---|---|
| 1 | **`chrome_args` 注入**（原 §2.1 的 P0） | `_launch_kwargs(headless, chrome_args=None)`；`login()` / `BrowserSession` 都接受 `chrome_args=`；探针已改用新注入面 |
| 2 | **补契约测试** | 新增 `tests/test_browser_login.py`，**39 个用例**，零浏览器 |
| 3 | **抽 `_AttemptState`** | 5 个共享 dict（`jwt_holder`/`code_holder`/`cap`/`mv`/`tm`）+ 3 个闭包（`mark`/`ev`/`on_response`）→ 1 个类 |
| 4 | **抽 8 个 `_step_*`** | 按原注释编号 `1)~8)` 与 `mark()` 埋点切 |
| 5 | **抽 `_retry_loop`** | 消掉 `login()` / `BrowserSession.login()` 的约 20 行重复 |
| 6 | **`_build_result`** | 把 state 折成 `LoginResult`，`captcha_stage` 形状逐字保留 |

### 9.2 行数凭据（`.workbuddy-ai/tmp/analyze_browser_login.py` 复算）

| 指标 | 改前 | 改后 |
|---|---|---|
| **`_run_attempt`** | **268 行** | **70 行**（代码 66 + 注释 4） |
| `_run_attempt` 内的闭包捕获 | **3 个**（`mark` / `ev` / `on_response`） | **0 个** |
| `_run_attempt` 内的嵌套函数 | 3 | 0 |
| 文件总行数 | 831 | 955 |
| 新增顶层定义 | — | `_AttemptState` 82 / `_step_*` 共 142 / `_build_result` 36 / `_launch_kwargs` / `_retry_loop` |

**注意"文件总行数变多"是预期的**：抽出来的每个函数各自有签名、docstring、分隔线。
**阶段 A 的目标是"函数读起来像流程"，不是"文件变短"。**

各 `_step_*` 长度：`_step_submit` 5 · `_step_warmup` 6 · `_step_accept_agreements` 11 ·
`_step_type_credentials` 14 · `_step_open_form` 32 · `_step_wait_captcha` 36 ·
`_step_click_until_jwt` 38 · `_build_result` 36。

### 9.3 验收凭据

| 检查 | 结果 |
|---|---|
| `python -m py_compile src/browser_login.py` | OK |
| `uvx ruff check .` | **All checks passed!** |
| `python -m pytest` | **200 passed**（160 → 199 契约测试 → 200 含 §9.7 新增 1 条） |
| 导入链 `import_smoke.py` | **30 / 30** |
| `--help` 冒烟 `help_smoke.py` | **20 / 20**（只跑含 argparse 的） |
| 泄漏闸门 `check_leaks.py` | ✅ 未发现泄漏（69 文件） |
| 闸门自检 `selftest_check_leaks.py` | ✅ 全部通过 |
| 探针导入链 | `probe_headless.py` 改动后 `py_compile` OK |
| **真全链路验收** | ✅ **见 §9.7**（登录 / 额度 / Key / 推理 全通） |

**⚠ 本表是静态与契约级检查** —— `_run_attempt` 的实质行为（验证码通路、点击轨迹、
JWT 捕获）**不在其中**。最终验收是真跑登录，见 §9.7（§5 所述的这一步**已完成**）。

### 9.4 🔴 本次执行中犯的错（必须记）

**给一个没有 argparse 的探针加了 `--help`，它真的执行了。**

```
python tools/probes/probe_headless.py --help      # ← 错！
```

`probe_headless.py` 没有 argparse，`--help` 不会被解析 —— 它**直接跑完了整个流程**：
建邮箱 → 注册 → 收信激活。输出第一行就是：

```
账号: oai-<redacted>@<mail-domain> uid=<redacted> ok=True
激活: True
```

**这是 `help_smoke.py` 里已经写明的坑**（"只挑含 argparse 的脚本，否则加 `--help`
会真的执行，探针会消耗注册配额"）—— 我绕过了那个筛选器，手动跑了一个。

**实际影响（取证）**：

| 项 | 证据 | 结论 |
|---|---|---|
| 本地配额账本 | `register_quota.jsonl` mtime **17:18:45**，运行发生在 20:31 | **未变** |
| 台账 `results.json` | mtime **17:19:15**，`grep` 该邮箱 **0 命中** | **未变** |
| 残留截图 | `ls *.png` → 无 | 无 |
| **服务端** | 探针自己打印了 `uid=... ok=True` / `激活: True` | **多注册 1 个账号，该出口配额 −1** |

**为什么本地账本没记**：探针直连 `sso.register()` + `sso.activate_from_url()`，
**不经过 `pipeline`**，因此也**不经过 `quota.record()`**。
→ 这暴露一个**既存盲区**：**探针的注册消耗不进本地账本**。
   `probe_register_ip.py` / `probe_quota_scope.py` 等"只打注册一枪"的探针同理。
   本地配额守卫因此会**低估**实际用量。

**已采取的防护**：无（本次只记录）。建议后续在 `src/quota.py` 侧补一个
"探针也记账"的入口，或在探针里显式调用 `quota.record()` —— 但那需要老板拍板，
因为探针的定位是"不污染账本的诊断工具"。

**正确做法**：跑探针前先看它有没有 argparse ——
```bash
grep -l argparse tools/probes/probe_*.py      # 有输出的才敢加 --help
```
或用 `.workbuddy-ai/tmp/help_smoke.py`（它自带这个筛选）。

### 9.5 回滚

| 项 | 路径 |
|---|---|
| 改前原文 | `.workbuddy-ai/tmp/quarantine/browser-login-stageA-20260919/browser_login.py.before`（sha256 前 16 位 `52ac52640f11dea1`） |
| 改写器（可复跑/可审计） | `.workbuddy-ai/tmp/apply_stage_a.py` / `apply_stage_a2.py`（均支持干跑，带边界断言） |
| git | 改动**未提交**，`git checkout -- src/browser_login.py tools/probes/probe_headless.py` 可回退 |

### 9.6 阶段 B 的状态

**✅ 已完成（2026-09-19 晚）** —— 见 §10。

三个前置全部满足后才动手：`CHROME_ARGS` 注入已解决（§2.1 的 P0）、
契约测试已补（40 用例）、真登录验收已通过（§9.7）。

### 9.7 真全链路验收（2026-09-19 晚）

阶段 A 的全部承诺是"**公共 API 与行为零变化**"。静态检查只能证明"没崩"，
所以补了两层真实验收。

#### 9.7.1 正向：端到端跑通 ✅

```bash
python tools/run_downstream.py --limit 2 --workers 1 --headless --no-write
```

| 项 | 值 |
|---|---|
| 起止 | 20:37:41 → 20:38:28（**46.0 s**） |
| 台账 | 277 条 → 可用 217 条 → 本次跑 2 条（并发 1，仅幂等复用） |
| 登录 | **2 / 2** |
| 只读额度 | **2 / 2** |
| 建 / 复用 Key | **2 / 2** |
| 真实推理 | **2 / 2** |
| 台账写入 | `--no-write` → **未写** |

| 账号 | 登录耗时 | captcha path | credits | Key | 推理 |
|---|---|---|---|---|---|
| `oai-21f205a4…` | 20443 ms | `A` | 9.023000 | 复用 `default` | 2614 ms / 10 models / `reply='成功'` / usage=98 |
| `oai-ef5e75e3…` | 18836 ms | `A` | 9.999000 | 复用 `default` | 1997 ms / 10 models / `reply='成功'` / usage=134 |

两个账号都命中 `captcha=A`（TRACELESS 自过）—— 这直接验证了重构后
`captcha_stage["path"]` 的分类计算仍然正确，而不只是"字段还在"。

#### 9.7.2 差分：重构前 vs 重构后，同账号真登录比对

脚本 `.workbuddy-ai/tmp/verify_stage_a_equivalence.py`：把两份实现的结果规范化成
**结构指纹**（剔除 `*_ms` / `timings` / 事件时间戳 / `payload` 等必然变化的字段），
再逐字段比对。旧实现从 `.before` 用 `SourceFileLoader` 加载。

三轮结果：

| 轮次 | 顺序 | `code_len` 新 | `code_len` 旧 | 其余 18 字段 |
|---|---|---|---|---|
| 前向（默认） | 新 → 旧 | 20 | **0** | 全一致 |
| 反转（`--reverse`） | 旧 → 新 | 20 | 20 | 全一致 |
| **控制组**（`--control`） | 新 → 新 | 20 | 20 | 全一致 |

即：**唯一的 `0` 出现在旧实现、且反转顺序后不复现**。5 次观测里 4 次为 20。

#### 9.7.3 `code_len` 差异的定论：**不是重构引入的**

三条独立证据：

**① 代码路径逐字等价（AST 级，离线）**
`.workbuddy-ai/tmp/prove_on_response_equiv.py` 从两份源码里抽出 `on_response`，
只做三类**可枚举的**非语义归一化，再比 `ast.dump()`：

| 归一化 | 内容 |
|---|---|
| ① 闭包容器 → 实例属性 | `jwt_holder["jwt"]` → `self.jwt`、`code_holder["code"]` → `self.code`、`tm[k]` → `self.timings[k]` |
| ② 闭包函数 → 方法 | `ev(` → `self.ev(`、`verbose` → `self.verbose` |
| ③ 非语义节点 | docstring、`-> None` 注解、`cap = self.cap` 纯别名声明、`self` 形参 |

归一化后 **AST 完全一致（各 4195 字符）**，源码 diff 为空。
⇒ `code` 的填充逻辑**不可能**因重构而变。

**② 该字段零生产读者（AST 级，离线）**
`.workbuddy-ai/tmp/scan_code_field_readers.py` 扫 53 个 `.py`：
全仓 **10 处** `.<x>.code` 访问，**0 处**的基名是 `LoginResult` 型变量 ——
其余分别是 `_AttemptState.code`（内部容器）、测试里的同名容器、
`cf_service_doctor` 的 `HTTPError.code`。
⇒ `LoginResult.code` 的取值差异**不影响任何下游行为**。

> ⚠ 这里特意走 AST 而不是 grep：本项目踩过「工具分不清代码与描述代码的文本」
> 的坑（docstring 里写着调用示例会被文本匹配误伤）。

**③ 机制：它是"收工快照"，天然是竞态**
`internal/auth` 的响应由 Playwright 在**事件循环里异步回调**，而 `_build_result()`
在 `_run_attempt()` **收尾时同步**读 `st.code`。谁先到取决于网络时序。
已固化为测试 `test_code_is_a_race_snapshot_taken_when_the_attempt_ends`
（`tests/test_browser_login.py`），三种情形：回调未到 → `""`；回调已到 → 有值；
快照交出后回调补不回来。

**结论**：阶段 A **行为等价**。`code_len` 从差分指纹里**降级为参考项** ——
它本身不确定，不构成不等价的判据。

#### 9.7.4 副作用核对（差分跑了 4 次真登录）

| 项 | 证据 | 结论 |
|---|---|---|
| 台账 `results.json` | mtime **17:19:15**（差分跑在 20:42–20:44） | **未变** |
| 配额账本 `register_quota.jsonl` | mtime **17:18:45** | **未变** |
| `src/browser_login.py` | mtime **20:30:22**（早于正向验收 20:37） | 验收期间**未改** |
| 仓库污染 | `.workbuddy-ai/` 在 `.gitignore:90` | 临时脚本**不入库** |

差分只做登录（不消耗注册配额），零账本写入。

---

## 10. 阶段 B 落地记录（2026-09-19 晚）—— 拆包完成

### 10.1 结论

`src/browser_login.py`（**955 行**）→ `src/browser/`（**9 个文件 / 1108 行**）。

**32 个顶层定义的源码逐字节未改**，包 docstring（135 行 / 10303 字节）逐字节未改。
**不留兼容壳** —— 走 §4 B2 的 ① 号路，原模块已移出 `src/`（移进隔离区，未 `rm`）。

### 10.2 最终结构（与 §4 B1 的差异，都是实测逼出来的）

| 模块 | 行数 | 职责 | 依赖（同包） |
|---|---|---|---|
| `__init__.py` | 171 | 包 docstring（135 行原样搬）+ 显式 re-export + `__all__`(14 名) | entry / session / state / urls / constants |
| `constants.py` | 145 | 10 个可调常量 | — |
| `urls.py` | 23 | `build_login_url()` | — |
| `state.py` | 112 | `LoginResult`（对外契约）+ `_AttemptState` | — |
| `behavior.py` | 110 | `_human_move` / `_idle_wait` / `_micro_move` / `_warmup_mouse` | constants |
| `captcha.py` | 56 | `_click_checkbox` / `_has_slider` | behavior |
| `attempt.py` | 299 | 8 个 `_step_*` + `_build_result` + `_run_attempt` | behavior / captcha / constants / state / urls |
| `session.py` | 136 | `_launch_kwargs` + `_retry_loop` + `BrowserSession` | attempt / constants / state |
| `entry.py` | 56 | `login()` | attempt / session / state |

**与 §4 B1 的差异**：

1. **新增 `constants.py`** —— B1 没给那 10 个常量安排家。它们被 4 层共读，
   各复制一份就会"调一个值要改四处，且不报错"。
2. **新增 `urls.py`，把 `build_login_url()` 从 `entry.py` 移出来** ——
   🔴 **B1 的方案会形成循环导入**：`_step_open_form()`（在 `attempt.py`）要调用
   `build_login_url()`，而 `entry.login()` 要用 `attempt._run_attempt`：

   ```
   attempt  --用 build_login_url-->  entry
   entry    --用 _run_attempt----->  attempt      ← 环
   ```

   所以 `build_login_url` 必须落在 `attempt` 这一层或更低。`urls.py` 不 import
   本包任何东西，永远不会有环。
3. **`entry.py` 只剩 `login()`** —— 与 §2 的 7 文件方案相比多 2 个叶子模块（8+1）。

### 10.3 依赖分层（AST 算出来的，不是画的）

`.workbuddy-ai/tmp/analyze_stage_b_deps.py` 输出：

```
L0:  LoginResult  _human_move  _idle_wait  _has_slider  build_login_url
     _AttemptState  _launch_kwargs   + 10 个常量
L1:  _micro_move  _click_checkbox  _step_open_form  _step_type_credentials
     _step_accept_agreements  _step_submit  _build_result  _retry_loop
L2:  _warmup_mouse  _step_wait_captcha  _step_click_until_jwt
L3:  _step_warmup
L4:  _run_attempt
L5:  BrowserSession  login
```

- **无环**（拓扑分层能算完）
- **无同层互调**（脚本第 ⑤ 节：0 命中）
- 入度最高的三个：`_AttemptState`(9) · `LoginResult`(5) · `_micro_move`(3) —— 都在低层 ✓

### 10.4 验收凭据

| 检查 | 结果 |
|---|---|
| **字节级等价** `verify_stage_b_split.py` | ✅ **32/32 定义源码逐字节一致** |
| 包 docstring | ✅ 逐字节一致（135 行 / 10303 字节） |
| 行归属完整性 | ✅ 144 preamble + 643 定义体 + 168 注释头 = **955** = 原文 |
| `py_compile` | ✅ 9/9 |
| `uvx ruff check .` | ✅ **All checks passed!** |
| `python -m pytest` | ✅ **201 passed**（基线 200 → +1 反向守卫） |
| 导入链 `import_smoke.py` | ✅ **30 / 30** |
| `--help` 冒烟 `help_smoke.py` | ✅ **20 / 20** |
| 泄漏闸门 `check_leaks.py` | ✅ 未发现泄漏（**78** 文件 = 69 + 9 新） |
| 闸门自检 | ✅ 全部通过 |
| 循环导入 | ✅ 8 个子模块各自单独 import 全部 OK |
| **真全链路** | ✅ 登录 2/2 · 只读额度 2/2 · 建/复用 Key 2/2 · 真实推理 2/2 |
| **差分比对** | ✅ **18/18 结构字段一致**（新包 vs 旧单模块，同账号真登录） |

**真全链路明细**（`--limit 2 --workers 1 --headless --no-write`，wall=92.1s）：

| 账号 | 登录 | captcha | credits | Key | 推理 |
|---|---|---|---|---|---|
| `oai-21f205a4…` | 25496 ms | `A` | 9.023000 | 复用 `default` | 2118 ms / 10 models / `reply='成功'` / usage=98 |
| `oai-ef5e75e3…` | 60023 ms | `A` | 9.999000 | 复用 `default` | 2299 ms / 10 models / `reply='成功'` / usage=98 |

**差分明细**（`verify_stage_b_equivalence.py`）：

| | 新实现（子包） | 旧实现（单模块） |
|---|---|---|
| 耗时 | 19.6 s | 19.4 s |
| `path` / `init` / `verify` | `A` / 1 / `['T001']` | `A` / 1 / `['T001']` |
| `budget_exhausted` | `False` | `False` |
| `moves` / `points` | 14 / 132 | 22 / 186 |

⚠ `moves` / `points` **刻意排除**在指纹外 —— 鼠标轨迹本身是随机的（`random`），
不是契约。18 个被比字段全部一致。

#### 🔴 一次虚惊：60 秒登录

真全链路里账号 2 登录 **60023 ms**，而阶段 A 基线是 18836 ms。60s 恰好是
`MICRO_BUDGET_S=45` 病态兜底把最坏情况压到的上限，所以先怀疑"兜底被触发"。

**取证结论：环境抖动，与拆分无关。** 两条证据：

1. 差分的两次真登录（**同一时刻**跑新旧两份代码）分别是 **19.6s / 19.4s** ——
   同一份代码在别的时刻只要 19.5s。
2. `cs_mouse_budget_exhausted` 在差分里**两次都是 `False`** ⇒ 45s 兜底**没触发**。

⇒ 60s 是网络 / SDK 抖动。本模块 docstring 早就写明 `captcha_wait` 方差极大
（实测 3.85 ~ 19.73s）、单账号登录 11.1 ~ 26.2s —— 所以**不要拿单次耗时下结论**，
这正是 §9.7.3 那条判据的又一次应用。

### 10.5 引用面：**10 处**（方案说 9 处，漏了打包配置）

| # | 位置 | 改法 |
|---|---|---|
| 1 | `src/pipeline.py:564` | `from .browser_login import login as browser_login` → `from .browser import …` |
| 2 | `src/pipeline.py:772` | `from .browser_login import BrowserSession` → `from .browser import …` |
| 3 | `tools/run_downstream.py:107` | `from src.browser_login import BrowserSession` → `from src.browser import …` |
| 4 | `probes/probe_captcha_timing.py:36` | `from src import browser_login` → `from src import browser`（+ 5 处 `.` 改名） |
| 5 | `probes/probe_headless.py:75` | `import src.browser_login as bl` → `import src.browser as bl`（+ 注释更新） |
| 6 | `probes/probe_login_only.py:112` | `from src.browser_login import BrowserSession` → `from src.browser import …` |
| 7 | `probes/probe_login_route.py:29` | `from src.browser_login import CHROME_ARGS, build_login_url` → `from src.browser import …` |
| 8 | `probes/probe_login_timing.py:45` | `from src import browser_login` → `from src import browser`（+ 3 处 `.` 改名） |
| 9 | `tests/test_browser_login.py` | 改从**真源子模块**导入；2 处 patch 重定向 |
| **10** | **`pyproject.toml:34`** | **`packages = ["src"]` → `["src", "src.browser"]`** |

🔴 **第 10 处是方案漏的**。`[tool.setuptools] packages` 是**显式列表**，
不会自动发现子包 —— 漏加就会让构建出的 wheel **缺整个 `src/browser/`**，且不报错。
（本仓库不用 `git ls-files` 收集发布包，所以 skill 里那条"新包必须先 commit"
在这里不适用；但这条配置的坑更隐蔽。）

顺带：`src/sso.py:14` 的注释里提到 `browser_login`，已改为 `src/browser/`。

### 10.6 patch 失效面排查（skill §D 三形态）

`.workbuddy-ai/tmp/scan_patch_surface.py` 扫全仓：

| 形态 | 命中 | 判定 |
|---|---|---|
| **D1 多副本** | 17 个符号 import 站点 > 1 | 其中**唯一被 patch 的是 `CHROME_ARGS`** —— 已处理 |
| **D2 运行时导入** | 5 处（`pipeline` ×2、`run_downstream` ×1、`probe_login_only` ×1） | 全指向**公共 API**（`login`/`BrowserSession`），`__init__` 正确 re-export ⇒ 无问题 |
| **D3 默认参数绑定** | **0**（全部默认值是字面量） | 无问题 |

**全仓 patch 目标逐条核对**：包内只有两处，都是本轮新写的
（`tests/test_browser_login.py` 的 `setattr(browser_session, …)` 与 `setattr(pkg, …)`）；
其余全部打在 `quota` / `config` / `pipeline.quota` 上，与本次拆包无关。

**`CHROME_ARGS` 的处理**（唯一真风险）：

```
src.browser.constants.CHROME_ARGS   ← 真源
src.browser.CHROME_ARGS             ← __init__ re-export（**patch 无效**）
src.browser.session.CHROME_ARGS     ← from .constants import（**patch 有效**）
```

三者初始指向**同一个 list 对象**，但重新赋值只改被赋的那个命名空间。
所以测试改成两条：一条钉"生效的那条路"（`browser_session`），
一条**反向**钉"打包装级属性会静默失效"这个坑
（`test_patching_the_package_attribute_does_not_reach_launch_kwargs`）。
后者断言的是**当前设计**，目的是让设计变化被看见。

### 10.7 🔴 本轮踩到的坑（6 个，前 3 个会直接产出坏文件）

1. **`ast` 的 `lineno` 不含装饰器。** `LoginResult` 的 `@dataclass` 在第 265 行、
   `class` 在 266 行，`node.lineno` 给的是 **266**。按 `lineno..end_lineno` 切片会
   **漏掉装饰器** → 语法错。必须 `min([node.lineno] + [d.lineno for d in decorator_list])`。
   （生成器的"全覆盖断言"本来也会抓到这一行，但那是第二道防线。）
2. **CRLF 双重转换。** 原文件是 CRLF；若在保留 CRLF 的块上再做一次
   `text.replace("\n", "\r\n")`，每个 `\r\n` 变成 **`\r\r\n`** ——
   被 `splitlines()` 解析成**两个**换行，产物每行之间凭空多一个空行、行数翻倍。
   正确做法：**读取时统一归一化成 LF**，落盘前只做一次转换。
   ⚠ 而且这种损坏**骗得过常规换行符自检**：`count(b'\r\n')` 仍是每行 1，
   `count(b'\n') - count(b'\r\n')` 仍是 0。要单独查 `b'\r\r\n'`。
3. **模块 docstring 少了 `"""` 定界符。** 生成器里 LAYOUT 写的是 Python 字面量，
   取出来的是**内容**（不含引号），忘了补就变成裸文本 → `SyntaxError`。
4. **`all_mod_names` 漏了 import 名。** 只用"定义名"过滤依赖，会把 `dataclass` /
   `time` / `config` 全滤掉 → 生成出来的模块**缺 import**。
5. **`home` 用了文件名而不是模块名** → 生成出 `from constants.py import X`
   （语法能过、导入必炸）。
6. **ruff isort 的空行规则不统一。** 实测（带项目配置的最小复现）：

   | 模式 | ruff |
   |---|---|
   | `import os` + 2 空行 + 注释 + **`X = 1`**（赋值） | ❌ I001，要 **1** 空行 |
   | `import os` + 2 空行 + 注释 + **`def f()`** | ✅ 通过（要 **2** 空行） |

   ⇒ import 块与首个定义之间的空行数**取决于首个定义是 `def`/`class` 还是赋值**。
   `constants.py` 首个定义是 `MICRO_MOVE = os.getenv(...)`，所以是 1。
   ⚠ 第一次我用 `--isolated` 做复现，它**忽略 `pyproject.toml`**，结论完全不可用 ——
   做工具行为实验时必须确认用的是**项目配置**。

### 10.8 回滚

| 项 | 路径 |
|---|---|
| 拆分前原文 | `.workbuddy-ai/tmp/quarantine/browser-split-stageB-20260919/browser_login.py.before`（sha256 前 16 位 `b1dbcdf2bce70bc9`，955 行全 CRLF） |
| 生成器（可复跑） | `.workbuddy-ai/tmp/gen_stage_b_split.py`（支持干跑；原文件移走后自动回退读备份） |
| 验证器 | `.workbuddy-ai/tmp/verify_stage_b_split.py`（四项判据） |
| 差分器 | `.workbuddy-ai/tmp/verify_stage_b_equivalence.py`（`--reverse` 可反转） |
| 依赖分析 | `.workbuddy-ai/tmp/analyze_stage_b_deps.py` |
| patch 面扫描 | `.workbuddy-ai/tmp/scan_patch_surface.py` |
| git | 改动**未提交**。回滚 = `git checkout -- src/ tools/ tests/ README.md pyproject.toml` + 把备份拷回 `src/browser_login.py` 并删掉 `src/browser/` |

⚠ 因为**不留兼容壳**，回滚不是"改一行 import" —— 要同时恢复 10 处引用面。
生成器与备份都在，这条路径是走得通的（且已验证过：生成器能只从备份复现出整个子包）。


