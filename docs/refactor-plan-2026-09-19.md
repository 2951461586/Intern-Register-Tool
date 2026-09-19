# 重构方案（2026-09-19）

> 本文只讲**改什么、为什么改、怎么验证**。所有结论都带 `文件:行号` 级证据，
> 未经验证的推测一律标注。
> 本文遵守 [`security-conventions.md`](security-conventions.md) 的占位符约定，
> 不含任何真实凭据 / 出口 IP / 实例子域。

---

## 0. 结论摘要

这个项目的**业务逻辑质量很高**——注释密度罕见，每条反直觉的决策都附了实测数据
（`workers` 边界、验证码两条通路、D1 读配额、配额窗口 24h 的由来）。
问题不在"想得不清楚"，而在**工程化外壳缺失 + 三处架构级的抽象错配**。

按投入产出排序，真正值得动的是这五件事：

| 优先级 | 问题 | 状态 | 一句话 | 证据 |
|---|---|---|---|---|
| **P0** | 池子无出口 IP 感知 | ✅ **已修**（阶段一） | 池子按 `slot` 分配租约，配额按 `出口 IP` 记，而池子不知道出口 IP | `src/proxypool.py` 全文零 `egress` 引用 |
| **P0** | 被封出口的重试风暴 | ✅ **已修**（阶段一 + 阶段三 #16） | 冷却 120s vs 配额窗口 24h，差 720 倍 | `proxypool.py:252` / `config.py:162` |
| **P1** | 零工程化配置 | ✅ **已修**（阶段二） | 无 lint / 无 type check / 无 pytest / 无 CI 测试 | `ls` 无 `pyproject.toml` 等 8 类文件 |
| **P1** | 两个巨型模块 | 🟡 **部分** | `browser_login.py` 831 行、`pipeline.py` 774 行 | `wc -l` |
| **P1** | 台账合并有两套实现 | ✅ 已确认 | `ledger.py` 说"只能有一处"，`restore_results.py` 另有四个函数 | `restore_results.py:56-107` |
| **P1** | ↳ 但两套**降级行为必须不同** | ✅ **已修正** | 直接换 `merge_records` 会丢 15 账号 / 18 字段 ⇒ 落地为 `merge_fragments` | §10.7 |

> **落地进度总览（截至 2026-09-19 收工）**：阶段一 4/4 ✅、阶段二 5/5 ✅、
> 阶段三 6/7 ✅（仅剩 #14 拆 `browser_login.py`，高风险）。
> 全量验收凭据见 §10.14。**两条 P0 已装弹且已拆除引信** ——
> 出口 IP 感知（`_ip_held` 互斥）与指数退避 + 跨进程状态持久化均已落地。

### 🔴 关于两条 P0 的状态：**已装弹，尚未击发**

必须说清楚：**这两条目前都还没有造成实际损失。**

依据是项目自己的运行记录（`.workbuddy-ai/memory/2026-09-19.md:849`）：

> - **无任何出口被判定封禁**（日志无冷却/`B0000` 记录）

以及 `:767`：

> 实际并发 = 注册 3 路（受 3 个出口槽位限制）

也就是说：**启用的 3 个槽位恰好是 3 个不同出口 IP**，所以同 IP 并发从未发生；
**也从未有出口被封过**，所以 120s 冷却从未被触发。

那为什么还是 P0？因为**两条都只需要一个操作动作就会击发**：

| 触发条件 | 后果 |
|---|---|
| 把 `7905` / `7906` 加进 `slots.txt` | 与 `7903` / `7901` 同 IP → 同 IP 并发立刻成立 |
| 任何一个出口真的撞上服务端配额 | 进入 120s 循环，开始每天重打约 720 次 |

而且第二条的触发概率**随规模上升**：出口越多、跑得越久，撞到配额的出口就越多。
当前"没发生"是**规模还小 + 恰好没有同 IP 槽位**两个巧合叠加的结果，
不是设计上排除掉了。所以优先级高，但**不是"正在流血"**——按"修好它以免将来
在某个跑批的晚上被打断"来理解，而不是"今天就在浪费"。

---

## 1. 现状画像（可核查）

```
src/       12 个 .py   3,577 行     库代码
tools/     28 个 .py   6,823 行     探针 / 闸门 / 运维 / 自检
run.py                 344 行       CLI 入口
README.md            100,735 字节   1,956 行
--------------------------------------------------
合计                 10,744 行
```

已入库文件 49 个（`git ls-files | wc -l`）。`results.json` 已被
`.gitignore:45` 的 `*.json` 正确排除。

**规模最大的五个文件：**

| 文件 | 行数 | 问题 |
|---|---|---|
| `src/browser_login.py` | 831 | `_run_attempt()` 单函数 274 行（432–705） |
| `src/pipeline.py` | 774 | `run_batch()` 内嵌 6 个闭包，混三种职责 |
| `tools/ops/cf_service_doctor.py` | 552 | 单文件 12 个 `check_*`，可接受 |
| `tools/ops/gen_mihomo_slots.py` | 514 | 节点解析 + 配置生成 + 探测，可拆 |
| `tools/gates/check_leaks.py` | 457 | 闸门，逻辑自洽，不建议动 |

---

## 2. 代理池（P0，最高优先级）

代理池是**本项目的规模化杠杆**——`proxypool.py` 自己写得很清楚：
"真正的约束是有多少个不同出口 IP，不是本地并发度"。
但当前实现里，**池子对"出口 IP"这件事一无所知**，这导致三个缺陷。

### 2.1 🔴 缺陷 A：分配单位是 slot，约束单位是 egress IP

**事实链条（三条，都可核查）：**

1. `proxypool.py` 的模块 docstring 第 33–40 行自己记录了实测结果：
   > **不同节点名 ≠ 不同出口 IP。** 实测 6 个槽位（6 个不同节点）只得到 **4 个**
   > 不同出口 IP —— 有两对节点共用了同一个后端出口。

2. 但 `_pick_locked()`（`proxypool.py:132-175`）的排序键只有
   `(self._uses[i], i)`，候选集只看 `self._free` 与 `self._cool_until`。
   全文件对 `slot_scope` / `SLOT_EGRESS_IPS` / `egress` 的引用数为 **0**。

3. `config.slot_scope()` 明明已经有 `端口 → 出口 IP` 的映射
   （`config.py:279-300`，来源 `.env` 的 `IR_SLOT_EGRESS_IPS`），
   **但只有 pipeline 在用它记账，池子的分配决策完全没用上。**

**后果：** 两个共用同一出口 IP 的 slot 会被**同时**租出去。
服务端看到的是"同一个 IP 上 2 路并发注册"，而配额是按 IP 累计的
→ 撞穿速度翻倍。

**⚠ 这条原则项目里已经写下来了，只是只落地了一半。**

`.workbuddy-ai/memory/2026-09-18.md:261-262`：

> **"配了几个槽位"≠"能并发几个"**，必须按真实出口 IP 去重。
> 3. **判据按真实出口 IP 去重，不按槽位号** —— 按槽位号去重会把同一出口打两次

这条结论**被应用到了"记账"**（`config.slot_scope()` 用出口 IP 当 scope，
`quota.status(scope=ip)` 按出口分别计数——这部分做得对），
**但没有应用到"并发分配"**（`ProxySlotPool._pick_locked()` 仍按 slot 分配）。

所以这不是"没想到"，而是**同一个洞察在一处落地、另一处漏了**。
`2026-09-19.md:832-833` 还记录了操作层的规避：

> | 7905 | … | 203.0.113.11 | ❌ 与 7903 **同 IP**，加了无增益 |
> | 7906 | … | 203.0.113.12 | ❌ 与 7901 **同 IP**，加了无增益 |
>
> （原表里的出口 IP 已按 `docs/security-conventions.md` 换成 TEST-NET-3 占位符；
> 真值只留在 `.workbuddy-ai/memory/` 内，该目录不入库。）

**"加了无增益"是人的判断，代码里没有对应的守卫。** 换个人、或半年后忘了这条，
把 7905 追加进 `slots.txt`（而"追加到末尾"正是 `2026-09-19.md` 推荐的做法），
守卫不会拦，缺陷立刻成立。**修 A1 的价值就是把这条人工纪律变成代码约束。**

`pipeline.py:674-684` 的 `accept=_slot_has_quota` 看似挡住了，但它
**是在拿租约之前判的**：两个线程可以同时通过 `used=38/40 < 40` 的检查，
各自拿到一个 slot，然后各自注册——**检查与使用之间存在竞态窗口**。
（`pipeline.py:705-714` 拿到租约后**又查了一次**，说明作者已经意识到这个窗口，
但补的这道检查仍然只查"自己这个 slot"，管不了"另一个 slot 是同一个 IP"。）

**修法（推荐 A1，改动局部）：**

把 slot 按出口 IP 分组，池子内部维护 `ip -> 已租出数`，同组内互斥：

```python
# 新增：slot -> 出口 IP（建池时从 config.slot_scope 一次性解析）
self._slot_ip = {i: config.slot_scope(url) for i, url in enumerate(slots, 1)}
self._ip_held = {}          # ip -> 当前已租出数

def _pick_locked(self, exclude, accept):
    ...
    cands = [i for i in avail
             if accept(i)
             # 🔴 同出口 IP 同时只允许 1 个租约 —— 配额是按 IP 记的
             and self._ip_held.get(self._slot_ip[i], 0) == 0]
```

`acquire()` 里 `self._ip_held[ip] = self._ip_held.get(ip, 0) + 1`，
`release()` / `report_*()` 里减回去。

⚠ **代价要说清楚**：这会让池子的**有效并发度从 `len(slots)` 降到
`不同出口 IP 的个数`**。在"6 节点 / 4 出口"的实测配置下，并发上限从 6 变 4。
**这是对的**——那 2 个多出来的 slot 本来就不提供额外配额，只是让撞穿更快。
但必须在启动时把这个数字打印出来，否则用户会以为"配了 6 个槽位能跑 6 路"。

`config.slot_scope()` 在端口未登记时**抛 ValueError**（刻意设计，见其注释）。
建池时调用它会把异常提前到启动阶段——这正好符合 `config.validate()` 的
"缺项在入口报错"原则，是**行为改善**而非回归。

**替代方案 A2（不推荐）**：保留 slot 级并发，改为在池子内部做**加权轮询**，
让同 IP 的 slot 权重减半。比 A1 复杂，且不能真正阻止同 IP 并发——只是降低概率。

---

### 2.2 🔴 缺陷 B：被封出口的"重试风暴"（冷却时长差 720 倍）

**事实链条：**

| 环节 | 时长 | 位置 |
|---|---|---|
| 槽位被 `B0000` 封后的冷却 | **120 s** | `proxypool.py:252`（`self.cooldown`，来自 `IR_PROXY_COOLDOWN`） |
| 服务端配额恢复窗口 | **24 h** | `config.py:162`（`REG_QUOTA_WINDOW_H`，实测 > 8.6h，取 24h 作保守上界） |

**比值 720 倍。**

**关键的第二步（这是缺陷真正的成因）：**
`report_banned()`（`proxypool.py:238-259`）**只设置冷却时间，不写任何配额记录**——
`grep -n "quota\." src/proxypool.py` 返回**空**，池子根本不碰配额台账。

而 `quota.record()`（`quota.py:198`）**只在注册成功时调用**
（`pipeline.py:229`，注释："只有成功才计入本地配额"）。

于是这个链条闭合了：

```
出口 IP 撞穿配额 → 注册返回 B0000 → report_banned(冷却 120s)
   ↓  （本地配额计数**没有增加**，因为失败不记账）
120s 后 → _cool_until 到期 → 该 slot 重新进入 avail
   ↓
accept=_slot_has_quota → quota.status(scope=ip).exhausted
   ↓  本地计数没涨（还是 38/40 之类）→ exhausted=False → **放行**
   ↓
这个已被封的出口被重新租出去 → 又打一次真实注册请求 → 又 B0000
   ↓
循环，每天约 720 次（24h / 120s）
```

**每次循环都是一次真实的、注定失败的 HTTP 请求**，而且服务端会把
"反复撞配额"看在眼里——这是**主动加深封禁**。

**⚠ 尚未观测到。** 上面是**机制推导**，不是实测。项目运行记录
（`.workbuddy-ai/memory/2026-09-19.md:849`）明确写着"无任何出口被判定封禁
（日志无冷却/`B0000` 记录）"——**这个闭环一次都还没跑起来过**，
因为还没有任何一个出口真的被封。

所以它的定性是：**逻辑上闭合、参数上严重失配、但尚未击发的缺陷。**
把它当 P0 的理由是"下次撞配额时它一定会发生"（120s 与 24h 的比值是算术，
不是估计），而不是"它正在发生"。

⚠ **验证方式也必须是"造一个必封的槽位"，而不是等它自然发生**：
在 `selftest_proxypool.py` 里直接调 `report_banned()` 然后断言
`accept` 在配额窗口内持续拒绝该 slot——这条用例现在必然失败。

**修法（B1）：让 `report_banned` 在配额台账里留下证据。**

最小改动是给 `quota.py` 加一个 `record_ban(email, scope)`，
写一条带 `"ban": true` 的记录，并让 `status()` 把它计入 `used`：

```python
# quota.py 新增
def record_ban(email: str = "", scope: str = "") -> None:
    """记一次**出口被封**。与 record() 分开，因为它不占"注册配额"语义，
    但必须参与"这个出口还能不能用"的判断。"""
    _append({"ts": time.time(), "email": email, "scope": scope, "ban": True})
```

然后 `status()` 里把 ban 记录也计入 —— 但**只计入 `exhausted` 判断，
不计入 `remaining`**（否则 `run.py` 会打印出一个假的剩余额度）。

⚠ 这里有个设计取舍要讲明：`used` 现在同时承担"已用额度"和"已确认不可用"
两个含义。**更干净的做法是分开**（`QuotaStatus` 加 `banned: bool` 字段），
但改动面更大。建议先按 B1 落地，把 `banned` 字段留作 P2。

**修法（B2，与 B1 互补）：冷却指数退避。**

`self._bans[slot]` 已经在记封禁次数（`proxypool.py:253`），
但**只用于统计和收尾告警**（`pipeline.py:757`），**没进冷却计算**。
改成：

```python
n = self._bans.get(lease.slot, 0)          # 已封次数（本次之前）
cool = min(self.cooldown * (2 ** n), 6 * 3600)   # 120 → 240 → 480 → ... 封顶 6h
```

这条**不需要动配额模块**，改 3 行，能立刻把重试次数从 720/天压到 ~6/天。
**建议 B2 先落地**（改动最小、收益立竿见影），B1 随后补上。

---

### 2.3 缺陷 C：池子状态纯内存，跨批次失忆

`grep -n "json\|write_text\|open(" src/proxypool.py` 返回**空**——
`_cool_until` / `_bans` / `_uses` 全部只活在进程里。

**后果：** 上一批刚被封的出口，下一批启动时**冷却状态归零**，
全部槽位立刻可租。配合缺陷 B，等于每启动一次就重打一轮。

**修法：** 把 `{slot: {cool_until_epoch, bans}}` 落到
`.workbuddy-ai/state/proxypool.json`，建池时读回。
⚠ 注意 `_cool_until` 现在用的是 `time.monotonic()`（`proxypool.py:252`），
**monotonic 不能跨进程比较**——持久化必须改用 `time.time()`。

---

### 2.4 缺陷 D：`exclude` 参数在生产代码里从未被使用

模块 docstring 规则 2 写着：

> **被 B0000 的槽位进冷却，不是永久拉黑**。但**同一个 worker 不再回到
> 它已经失败过的槽位** —— 它必须**向前推进**。

但 `grep -rn "\.acquire("` 显示：`pipeline.py:683` 与
`recover_activation.py:169` **都没有传 `exclude`**。
唯一传 `exclude` 的是 `tools/selftests/selftest_proxypool.py:150,155`——
**自检在验证一个生产代码从不使用的参数。**

这不是 bug（`exclude` 默认为空，行为正确），而是**文档承诺与实现脱节**。
处置二选一：

- **删掉这条规则**，承认"一个账号失败后由冷却机制兜底"；
- 或**真正用起来**：在 `stage_register` 内做重试循环时，把已试过的 slot 传进 `exclude`。

⚠ 推荐**前者**——当前架构里 `producer()` 每个账号只拿一次租约、不做重试
（`pipeline.py:649-728`），`exclude` 没有使用场景。留着是误导。

---

### 2.5 缺陷 E：`IR_PROXY_PREFLIGHT` 是死配置，且它承诺的行为很关键

`config.py:209` 定义了 `IR_PROXY_PREFLIGHT`，注释写着：

> 🔴 为什么默认开：槽位实例是**独立的前台进程**，很容易"配置还留着、进程已经没了"。
> 那种状态下每条注册记录都会以**代理连接错误**收场，而在这个项目里
> "注册全失败"最容易被误读成"换 IP 也不行 / 还在封" —— 结论完全错。

`grep -rn "IR_PROXY_PREFLIGHT\|preflight" src/ tools/ run.py --include=*.py`
（排除 config.py 自身）→ **返回空**。

**这段推理是对的，代码没实现。** 这是本项目里**唯一一个"注释比代码更清醒"的地方**，
而且它描述的失败模式（把"槽位进程死了"误读成"换 IP 无效"）正是最贵的那种误判。

**修法：** 在 `build_pool()` 或 `run_batch()` 开头，对每个 slot 的本地端口做
TCP 连通检查（毫秒级，`socket.create_connection` 超时 0.5s），
不通的直接从槽位列表剔除并打印。**这不是新功能，是补上已经写好的设计。**

同类死配置（一并清理）：

| 常量 | 位置 | 状态 |
|---|---|---|
| `IR_PROXY_PREFLIGHT` | `config.py:209` | 零引用（**建议实现，不删**） |
| `MAIL_LIST_MIN` | `config.py:128` | 零引用（`MAIL_LIST_LIMIT` 有被 `tempmail.py:133` 用） |
| `SlotLease.last_ip` | `proxypool.py:69` | 零引用，永远是空串 |
| `SlotLease.uses` | `proxypool.py:68` | 只在构造时赋值（`proxypool.py:211`），无消费者 |

---

### 2.6 代理池改造优先级

```
P0  ① B2 冷却指数退避          3 行，装上保险（720 次/天 → ~6 次/天）
P0  ② E  补上端口预检           设计已写好，只是没实现
P0  ③ A1 按出口 IP 分组互斥     中等改动，把人工纪律变成代码约束
P1  ④ B1 ban 记录进配额台账     让 accept 真正拦得住
P1  ⑤ C  池子状态持久化         注意 monotonic → time.time()
P2  ⑥ D  删掉 exclude 或真正启用它
```

**验证方式（每条都要能证伪）：**

- ① 用 `selftest_proxypool.py` 的现成模式加用例：同一 slot 连封 3 次，
  断言冷却序列是 `120 → 240 → 480`。
- ③ 加用例：构造 4 个 slot，其中 `slot1`/`slot2` 映射到同一出口 IP
  （用 `IR_SLOT_EGRESS_IPS` 注入），断言并发 acquire 时
  **两者不会同时被租出**。这条用例**现在必然失败**——那就是它存在的意义。
- ⑤ 加用例：建池 → 封 1 个 → 序列化 → 反序列化 → 断言冷却仍在。

---

## 3. 架构设计（P1）

### 3.1 两个巨型模块

**`browser_login.py`（831 行）** 的 `_run_attempt()` 是 274 行的单函数
（`browser_login.py:432-705`），里面串了六件事：

1. 页面导航与元素等待
2. 人类行为模拟（贝塞尔曲线移动、打字节奏）
3. 验证码通路判定（Path A TRACELESS / Path B 点击降级）
4. 点击重试循环（`MAX_CLICKS_PER_ATTEMPT = 3`）
5. JWT 提取
6. 统计埋点（`mark()` / `ev()` 闭包）

**建议拆法：**

```
src/browser/
    __init__.py
    session.py      BrowserSession（进程生命周期、启动、关闭）
    behavior.py     人类行为：_human_move / _micro_move / _idle_wait / _warmup_mouse
    captcha.py      验证码：通路判定、_click_checkbox、_has_slider、降级逻辑
    login.py        _run_attempt 的编排（导航 → 行为 → 验证码 → 取 JWT）
```

拆完后 `login.py` 的 `_run_attempt` 应该降到 60~80 行——**它变成"读起来像流程"**
而不是"读起来像实现"。

⚠ **拆这个模块的风险最高**：文件头那 145 行注释记录了大量实测结论
（最小化注入、F001 归因、两条通路不能混比）。**拆的时候注释必须跟着代码走**，
不能留在 `__init__.py` 里。这是本项目最有价值的资产。

**`pipeline.py`（774 行）** 的 `run_batch()` 内嵌 6 个闭包：
`make_log` / `consumer_worker` / `_settle_lease` / `_note_register_result` /
`producer` / `_slot_has_quota`。

更值得关注的是**配额决策散在三处，判据和处置各不相同**：

| 位置 | 时机 | 判据 | 处置 |
|---|---|---|---|
| `pipeline.py:505-514` | 开跑前 | `st.used + count > st.limit` | 裁小 `count` |
| `pipeline.py:674-714` | 拿租约前/后 | `quota.status(scope).exhausted` | 标 `skipped` |
| `pipeline.py:623-647` | 每个账号后 | 连续 2 个 `B0000` | 置 `quota_hit` 事件 |

**同一个问题"要不要继续跑"有三种答案。** 这在逻辑上是有意为之（三处覆盖
不同时间尺度，注释也讲清楚了），但**认知成本高**：想知道"这批到底会不会被拦"，
必须同时读三个地方。

**建议：** 抽一个 `QuotaGovernor` 类，把三处决策收进去，对外只暴露
`allow(count) -> int`（开跑前）和 `check_slot(scope) -> bool`（运行中）、
`note_result(rec)`（结果反馈）。**不改变任何判据**，只是把决策集中。
收益是 `run_batch` 能瘦 80~100 行，且三条判据并排可读。

### 3.2 用错误文本当控制流

`is_quota_block(text)` 是 `QUOTA_MSG_CODE in text` 的字符串匹配
（`pipeline.py:88-90`），`QUOTA_MSG_CODE = "B0000"`（`pipeline.py:80`）。

`run.py:322` 也是同一个模式：`[r for r in bad if "B0000" in (r.error or "")]`。

**风险：** `AccountRecord.error` 是**拼接出来的自由文本**——
例如 `pipeline.py:241` 写 `f"register: {ex}"`，`ex` 里可能包含任何东西。
一旦某条失败信息里**恰好**含 `B0000`（比如某个 URL、某个 ID），
就会被误判成配额触顶。**注释里已经记录过这类误判**：

> `pipeline.py:637-641`：主动中止的记录 `error` 文本里**也含 B0000（是引用，
> 不是服务端返回）**，不排除就会被当成新证据重复计数。

**这已经是踩过一次的坑**，当时的修法是加一个 `elif rec.status == "skipped": pass`
把它排掉。但这是**打补丁**，不是修根因——下一个含 `B0000` 的文本还会再犯。

**建议：** `AccountRecord` 加一个结构化字段
`error_kind: str = ""`（取值 `""` / `"quota"` / `"network"` / `"browser"` / …），
在**抛出点**打标（`stage_register` 里 `reg.msg_code` 是结构化的，
`pipeline.py:220` 已经在用它拼 detail——**改成存字段而不是拼字符串**）。
`is_quota_block()` 降级为兜底，优先读 `error_kind`。

这条改动小、收益明确，且能顺手让 `_settle_lease`（`pipeline.py:604`）
的三种分支判定从"猜文本"变成"读字段"。

### 3.3 配置模块的副作用与横切关注点

**问题 1：`config.py` 在 import 时执行副作用。**

`config.py:36` 的 `_load_dotenv(...)` 是模块级调用，
`config.py:276` 的 `SLOT_EGRESS_IPS = _parse_slot_egress(...)` 也是。
后果是**测试无法在不污染 `os.environ` 的前提下导入 config**——
`selftest_quota.py` 只能靠 `IR_QUOTA_STATE` 环境变量做隔离（见 `quota.py:74-80`），
这正是这个设计逼出来的绕路。

**建议：** 保留模块级加载（对生产是便利），但把**解析结果**改成惰性：
`SLOT_EGRESS_IPS` 用 `@lru_cache` 的函数替代模块级常量。
这样测试可以 `cache_clear()` 后重载。

**问题 2：脱敏是横切关注点，却住在 config 里。**

`redact_url()` / `redact()`（`config.py:372-397`）与配置毫无关系，
但 `security-conventions.md` 第 2.4 条要求**所有输出边界**都必须过它们。
结果每个工具都得 `from src import config` 才能脱敏——**为了脱敏而耦合配置**。

**建议：** 新建 `src/redact.py`，`config.py` 里保留 re-export 以免破坏调用方
（⚠ 但这会引入"兼容壳"——见 §5 的反模式提醒，所以**建议一次性改完所有调用点**，
不留壳。调用点不多，`grep -rn "redact"` 可数）。

---

## 4. 目录结构（P1）

### 4.1 `tools/` 平铺 28 个文件，混了 6 类职责

| 类别 | 数量 | 文件 |
|---|---|---|
| 探针 | 13 | `probe_*.py` |
| 闸门 | 3 | `check_leaks` / `install_hooks` / `selftest_check_leaks` |
| 自检 | 4 | `selftest_*.py` |
| 运维 | 4 | `proxypool_ctl` / `cf_service_doctor` / `gen_mihomo_slots` / `migrate_quota_scope` |
| 数据操作 | 4 | `export_keys` / `restore_results` / `recover_activation` / `run_downstream` |
| 密钥 | 1 | `check_keys_alive` |

**建议结构：**

```
tools/
    probes/        13 个 probe_*.py   （一次性诊断，跑完就丢）
    gates/          check_leaks / install_hooks / selftest_check_leaks
    ops/            proxypool_ctl / cf_service_doctor / gen_mihomo_slots
    data/           export_keys / restore_results / recover_activation / migrate_quota_scope
    run_downstream.py                （保留在顶层：它是第二个入口）
tests/              4 个 selftest_*.py 迁入，改 pytest 风格
```

⚠ **`probe_*` 的定位要明确。** 它们 13 个文件 2,100+ 行，是**一次性诊断脚本**，
不是生产代码。建议在 `tools/probes/README.md` 里写清：每个探针
"回答什么问题、什么时候该跑、跑完看哪个数字"。**否则它们会腐烂成考古现场。**

### 4.2 README 100KB，是"设计文档 + 事故档案 + 操作手册"三合一

1,956 行 / 100,735 字节。问题不只是长，而是**与源码注释内容重复**：
`browser_login.py:46-85` 的验证码通路表格，与 README 里的表格是同一份数据。

**建议：**

```
README.md                    ~150 行：这是什么、怎么跑起来、去哪看细节
docs/
    architecture.md          两段式流水线、阶段划分、并发模型
    proxy-pool.md            槽位池设计、出口 IP 与配额的关系、已知缺陷
    measurements/            实测数据（workers 边界、验证码通路、D1 读配额）
    incidents/               事故档案（泄漏、台账缩水、误判）
    security-conventions.md  （已存在）
```

**单一事实来源原则：** 实测数据放 `docs/measurements/`，
源码注释**引用**它（`见 docs/measurements/captcha-paths.md`）而不是复述。
⚠ 但这与 `browser_login.py` 的设计初衷（"注释必须自带结论，否则改代码的人不会去翻文档"）
**存在冲突**。取舍建议：**结论与判据留在注释**（改代码的人必须看到），
**原始数据表移到 docs**（只有要复测的人才需要）。

---

## 5. 代码规范（P1）

### 5.1 零工程化配置 —— 这是最大的"欠账"

`ls -a | grep -iE "pyproject|setup.py|setup.cfg|tox|pytest|ruff|flake8|mypy|pre-commit|Makefile|editorconfig"`
→ **全部为空**。

具体缺失：

| 缺什么 | 后果 |
|---|---|
| `pyproject.toml` | 无法 `pip install -e .`，22 个文件各自 `sys.path.insert` |
| ruff / flake8 配置 | 无自动格式化、无未使用导入检查 |
| mypy / pyright 配置 | 项目**大量使用** `dict \| None` / `list[str]` 新式标注，却无人校验 |
| pytest 配置 | `tests/` 不存在，测试无法被 CI 发现 |
| `requirements-dev.txt` | `requirements.txt` 只有 3 行运行依赖 |
| Makefile / task runner | 每个命令都要手打 `python tools/xxx.py --yyy` |

**建议按此顺序补（从收益最高开始）：**

1. **`pyproject.toml` + `pip install -e .`** —— 消掉 22 处 `sys.path.insert`。
   证据：`grep -rln "sys.path.insert" tools/ src/ run.py` 返回 22 个文件。
2. **ruff**（同时替代 flake8 + isort + 部分 pyupgrade）—— 一行配置。
   ⚠ 首跑会有大量告警（这个项目的行宽明显超 79/88）。**建议只开
   `E,F,I,UP,B` 且 `line-length = 100`**，把历史告警用 `# noqa` 或
   per-file-ignores 压住，**不要一次性大改格式**——那会让 diff 淹没真实改动。
3. **pytest + `tests/`** —— 见 §5.2。
4. **CI 补 lint + test** —— 现在 `.github/workflows/` 只有 `secret-scan.yml`。

### 5.2 自检是手搓框架，应该收编进 pytest

4 个 `selftest_*.py`（`selftest_check_leaks` 192 行 / `selftest_merge` 175 /
`selftest_proxypool` 263 / `selftest_quota` 373）合计约 **1,000 行**，
但每个都自带一套 `check(name, cond, detail)` + `PASS/FAIL` 列表 + `main()` 返回退出码。

**这些自检的质量很高**——`selftest_check_leaks.py` 做的是**变异验证**
（造含假凭据的仓库，断言闸门非 0 退出），这是很多成熟项目都不做的事。
问题只在**外壳**：

- 不是 pytest 风格 → 无法用 `-k` 过滤、无 fixture、无参数化
- 无法与 CI 的测试报告集成
- `selftest_check_leaks.py` 里的 `load_gate()` 用**运行时拼接**构造假凭据
  （`"203.0.114" + "." + "5"`）—— 这是为了不被自己的闸门咬到，
  **非常聪明但极难维护**。pytest 下可以用 `tmp_path` fixture 更干净地做到。

**建议：** 保留 `selftest_check_leaks.py` 的变异验证逻辑（它是闸门的证明），
其余三个迁进 `tests/test_ledger.py` / `test_proxypool.py` / `test_quota.py`，
用 pytest 重写外壳（**不改断言**）。

⚠ **`selftest_check_leaks.py` 建议单独留在 `tools/gates/`**——
它必须能在**没有安装 dev 依赖**的环境里跑（CI 的 `secret-scan` job
只装了 Python 3.11，没装 pytest）。这是它的设计约束，不要破坏。

### 5.3 重复实现：台账合并有两套（规范说了，代码没做到）

`ledger.py` 的模块 docstring 第 11-12 行：

> 所以"合并而不是覆盖"这条规则必须**只有一处实现**，被所有会写台账的工具复用。

`run_downstream.py` 做到了——它老老实实 `from src import ledger`
（`run_downstream.py:293,418,423`），注释里还专门写了"为什么要复用"。

**但 `restore_results.py` 没有。** 它自带四个函数：

| `restore_results.py` | `ledger.py` 对应物 |
|---|---|
| `_rank()` (行 56) | `rank()` (行 22) |
| `_richness()` (行 83) | **已被 ledger 明确否决** |
| `_better()` (行 93) | `merge_records()` 的 rank 分支 |
| `_fill_missing()` (行 101) | `merge_records()` 的并集分支 |

**最讽刺的一点：** `ledger.py:52-61` 的注释**专门解释了为什么 richness 是错的**：

> 也试过"比非空字段个数"（richness），同样是启发式：两边字段数**相等**时
> 照样丢字段（自测 T9 当场抓到）。并集没有这个漏洞 —— 它不靠猜，
> 数学上保证字段只增不减。

**而 `restore_results.py:98` 还在用它**：
```python
return a if _richness(a) >= _richness(b) else b
```

也就是说：**同一个项目里，一处记录"这个方法有洞、已废弃"，
另一处还在用。** 这不是风格问题，是**规范失效**。

**建议：** 把 `restore_results.py` 的合并逻辑改为调用 `ledger.merge_records()`。
它多出来的能力（读 CSV、从 `.workbuddy-ai/tmp/` 收集多个来源、
`is_account()` 的判据）是**采集层**，应该保留；
但**"同 email 留哪条"的决策必须交给 ledger**。

> 🔴 **2026-09-19 落地时被数据推翻，见 §10.7。** 上面这条建议方向对（决策必须
> 收进 `ledger`），但**不能换成 `merge_records`** —— 实测 15 个账号丢 18 个字段。
> 最终落地为 `ledger.merge_fragments()`：规则收进 `ledger`，但**保留独立的降级行为**。
> 教训：§5.3 的论证基于"两处实现重复"，而**重复≠等价**，改之前必须跑等价性验证。

⚠ `restore_results.py:60-80` 的 `is_account()` 判据
（区分"流水线结果 / 导出 CSV / 验证码计时实验 / 失败注册"四类）
**非常有价值**，注释里记录了"差点把台账从 53 条灌成 93 条"。
这个函数应该**上移到 `ledger.py`** 或 `src/records.py`，
因为"什么算一个账号"是和"怎么合并账号"同级的领域规则。

### 5.4 代理串解析重复

| 实现 | 位置 |
|---|---|
| `config.proxies()` | `config.py:303-327`（正统，返回 `{"http":…, "https":…}`） |
| `probe_proxy.parse_proxy()` | `probe_proxy.py:67-78`（**同源逻辑，第二份**） |
| 手搓 dict | `probe_proxy.py:93`、`probe_register_ip.py:77` |
| 手搓 dict | `gen_mihomo_slots.py:119-120` |

`parse_proxy` 与 `config.proxies` 的差异只有返回值形态（str vs dict），
**逻辑完全一致**（都是 `://` 判断 → `split(":")` → 4 段/2 段分支）。

**建议：** `parse_proxy` 改为 `config.proxies(raw)["http"]` 的薄封装，
或直接在工具里用 `config.proxies()`。

---

## 6. 重构路线图

### 阶段一：止血（P0，建议立即做）

| # | 动作 | 改动量 | 验证 |
|---|---|---|---|
| 1 | 冷却指数退避（`proxypool.py` 3 行） | 极小 | 新用例：连封 3 次 → `120/240/480` |
| 2 | 实现端口预检（`IR_PROXY_PREFLIGHT`） | 小 | 手动：杀一个槽位进程 → 启动时应打印"剔除 N 个" |
| 3 | 按出口 IP 分组互斥 | 中 | 新用例：同 IP 两 slot 不同时租出（**当前必失败**） |
| 4 | 清理 4 处死代码 | 极小 | `ruff --select F401,F841` |

**阶段一完成后应能观察到：** 被 B0000 的出口不再每 120s 被重投一次；
配了 6 槽位但只有 4 个出口时，并发上限**诚实地**降到 4 并打印出来。

### 阶段二：补工程化（P1）

| # | 动作 | 依赖 |
|---|---|---|
| 5 | `pyproject.toml` + `pip install -e .`，消 22 处 `sys.path.insert` | 无 |
| 6 | 引入 ruff（保守规则集，不重排历史格式） | 5 |
| 7 | `tests/` + pytest，迁 3 个 selftest | 5,6 |
| 8 | CI 加 lint + test job（保留 secret-scan 独立） | 7 |
| 9 | 抽出 `src/redact.py`，一次性改完所有调用点 | 6 |

### 阶段三：结构治理（P1/P2）

| # | 动作 | 风险 |
|---|---|---|
| 10 | ~~`restore_results.py` 改用 `ledger.merge_records()`~~ → 抽 `ledger.merge_fragments()` | ✅ 已完成，**做法已改**（见 §10.7） |
| 11 | `tools/` 分 5 个子目录 + 写 `probes/README.md` | ✅ 已完成（见 §10.8） |
| 12 | `AccountRecord.error_kind` 结构化，替换文本匹配 | ✅ 已完成（见 §10.9） |
| 13 | 抽 `QuotaGovernor` | ✅ 已完成（见 §10.10） |
| 14 | 拆 `browser_login.py` → `src/browser/` | **高**（注释是核心资产）→ **阶段 A 已落地**（文件内重构），拆包待定：[`refactor-14-browser-split-plan.md`](refactor-14-browser-split-plan.md) |
| 15 | README 拆分到 `docs/` | 部分完成（见 §10.11） |
| 16 | 池子状态持久化（`monotonic` → `time.time()`） | ✅ 已完成（见 §10.12） |

---

## 7. 已知结论与误报排除

> 这一节的作用是**防止下一轮审计重查同一批东西**。每条都写清"查过了、结论是什么"。

### 7.1 已查实、无需再查

| 项 | 结论 | 依据 |
|---|---|---|
| `results.json` 是否会误入库 | ✅ 已被 `.gitignore:45` 的 `*.json` 排除 | `git check-ignore -v` 返回命中行 |
| 槽位号能否当配额 scope | ✅ 已用出口 IP 替代（做对了） | `config.slot_scope()` / `quota.status(scope=)` |
| 同 IP 槽位是否值得加 | ✅ 已判定"无增益"（人工纪律） | `memory/2026-09-19.md:832-833` |
| 闸门 `check_leaks.py` 是否有效 | ✅ 有变异验证（`selftest_check_leaks.py` + CI） | `.github/workflows/secret-scan.yml:34` |
| `run_downstream.py` 是否复用台账逻辑 | ✅ 复用，未重复实现 | `run_downstream.py:293,418,423` |
| 是否存在硬编码本机绝对路径 | ✅ `src/` 与 `tools/` 均无（`CHROME_PATH` 走 env 且有默认值） | `grep` 返回空 |

### 7.2 被排除的误报（**重要：别重新报一遍**）

**① AST 静态扫描的"读而从未写"字段：28 个报告全部是误报。**

用 AST 按 `ast.Attribute` 的 `Load` / `Store` 统计读写（技能
`deep-codebase-audit-landing` §"State fields: read-but-never-written" 的标准手法），
在 `src/` 上得到 **28 个**"有消费者但永不赋值"的字段：

```
AccountRecord.stages / .timings      ApiKey.id / .name / .key
ChatResult.ok / .text / .finish_reason
LoginResult.ok / .reason / .cookies / .captcha_stage / .timings
Mail.id / .to_address / .from_address / .extracted_json / .received_at
QuotaStatus.used / .limit / .window_h / .oldest_ts / .ts_sorted
RegisterResult.ok / .msg_code / .msg
SlotLease.slot / .url
```

**全部是误报。** 原因正是技能里列的四种失效构造之一：
这些字段都是**通过构造函数关键字**赋值的
（`AccountRecord(created_at=…)`、`SlotLease(slot=…, url=…)`、
`QuotaStatus(used=…, limit=…)`），而构造函数关键字在 AST 里是
`ast.keyword` 节点，**不是 `Attribute` + `Store`**。

⇒ **教训：这个判据只在"字段靠属性赋值到处写"的类上成立。**
本项目这几个 dataclass 恰好是反例。**不要把这 28 个当缺陷。**

**② 12 个"零读零写"字段：只有 3 个是真的。**

同样口径下报出 12 个既无读也无写的字段，逐个核验后：

| 字段 | 真实状态 |
|---|---|
| `SlotLease.acquired_at` | ✅ **真的死**（`field(default_factory=…)` 赋值后全库零引用） |
| `SlotLease.uses` | ✅ **真的死**（仅 `proxypool.py:211` 构造时赋值，零读取） |
| `SlotLease.last_ip` | ✅ **真的死**（默认 `""`，从未赋值、从未读取） |
| `AccountRecord.created_at` | ❌ 误报——经 `asdict()` 序列化，且 `quota._parse_created_at()` 会读它 |
| `ApiKey.masked_key` / `.created_at` | ❌ 误报——构造关键字赋值 |
| `ChatResult.model` / `.usage` / `.reasoning` | ❌ 误报——构造关键字赋值 |
| `LoginResult.code` | ❌ 误报——构造关键字赋值 |
| `Mail.subject` / `.body` | ❌ 误报——构造关键字赋值 |

⇒ **净结果：`SlotLease` 上 3 个死字段**，与 §2.5 的 grep 结论一致。
**两条独立方法（grep + AST）得到同一结论，这才是可以动手的依据。**

⚠ 验证脚本留在 `.workbuddy-ai/tmp/astscan.py`（未入库）。
下次要重跑，**先读本节**，不要从零再推一遍这 28 个误报。

### 7.3 需要业务/设计决策、本方案不擅自定的

| 项 | 为什么需要人来定 |
|---|---|
| `SlotLease` 三个死字段是删还是补功能 | `last_ip` 显然是"想做但没做"（模块 docstring 强调过"节点名≠出口 IP"）；删掉等于放弃那个意图，补上则是新功能。**这是意图问题，不是代码问题。** |
| `used` 是否该拆分"已用额度"与"已确认不可用" | §2.2 的 B1 会把两个语义压进同一个字段。干净做法是加 `banned: bool`，但改动面更大——**取决于愿不愿意现在付这个成本**。 |
| `IR_PROXY_PREFLIGHT` 是补实现还是删配置 | 注释里的推理是对的（槽位进程死了会被误读成"换 IP 无效"），但补实现要新增一个探活函数。**取决于是否认可那个失败模式的代价。** |
| `exclude` 是删还是启用 | 见 §2.4。启用需要先给 `stage_register` 加重试循环，那是行为变更。 |

---

## 8. 明确不建议做的事

**① 不要重构 `tools/gates/check_leaks.py`（457 行）。**
它的设计约束（fail-closed、两层判据、项目自带模式、变异自检）每一条都是
事故换来的，注释里写得明明白白。**它丑但正确**，动它风险远大于收益。

**② 不要引入 `python-dotenv` / `pydantic-settings` / `click` 等依赖。**
`config._load_dotenv`（`config.py:13-33`）是刻意手写的，
`config.py:14` 写明"不引 python-dotenv 依赖"。`run.py` 的 argparse 也够用。
**这个项目依赖极少（3 行）是优点，不是欠账。**

**③ 不要给 `docs/` 和源码注释做"去重"。**
`browser_login.py` 那 145 行头部注释记录了"为什么最小化注入"、
"为什么 micro_move 保留"、"为什么无头可用"——这些是**改代码时的护栏**。
移到 docs 里，改代码的人不会去看。**保留重复，但让 docs 成为数据的单一来源。**

**④ 不要追求 100% 测试覆盖。**
这个项目的核心是**与外部服务对抗**（风控、配额、验证码），
真正的验证方式是**探针跑真实请求**，不是 mock。测试应该只覆盖
**纯逻辑**（台账合并、配额计算、池子调度、脱敏），
这部分恰好是现有 4 个 selftest 覆盖的——**方向是对的，继续加深即可**。

**⑤ 不要在没有回归基线的前提下动 `pipeline.run_batch`。**
它内嵌 6 个闭包、共享 5 个可变状态（`results` / `q` / `quota_hit` /
`_quota_streak` / `print_lock`），且**并发正确性依赖注释里的推理**
（例如"配额检查必须在 gate() 之后"）。
任何重构**先补一个能跑的端到端基线**（`--count 4 --workers 2` 的记录），
再动。

---

## 9. 一句话总结

**业务逻辑值得信任，工程外壳欠账，代理池有一处抽象错配 + 一处参数失配——
两条都已装弹，只是还没击发。**

最该先做的不是"重构"这个大词，而是 §2.2 的 3 行冷却退避和 §2.5 的端口预检：
前者把"每个被封出口每天重打 720 次"变成 6 次，后者补上一个**已经写好理由
却没有实现**的检查。两条加起来不到 30 行。

⚠ 但要说清楚：**它们目前都没有在造成损失**（见 §0 末尾）。
按"趁现在装上保险"理解，不要按"救火"理解——如果把它们当成紧急故障去处理，
会为了一个尚未发生的问题改动刚稳定下来的池子逻辑，那是更大的风险。

真正**正在**发生的问题只有一个：**工程化外壳为零**（无 lint / 无 type check /
无 pytest / 无 CI 测试）。它不制造故障，但让上面每一条改动都更贵、更险。

---

## 10. 落地记录（2026-09-19 当天完成阶段一 + 阶段二）

### 10.1 阶段一（代理池止血）—— 4/4 完成

| # | 项 | 落地位置 | 关键实现 |
|---|---|---|---|
| A1 | 冷却指数退避 | `src/proxypool.py` `report_banned()` | `cool = min(cooldown * 2**min(n,16), cooldown_max)`；120→240→480…封顶 21600s（`IR_PROXY_COOLDOWN_MAX`）|
| A2 | 端口预检 | `check_slots_alive()` + `build_pool()` | 毫秒级 TCP 连通检查；全不通抛 `AllSlotsDead`（**刻意不静默退回单代理**）|
| A3 | 同出口 IP 互斥 | `_pick_locked()` + `_ip_held` | 按"在外租约数"计数；候选集过滤（**不放进 `accept`**，否则 `NoEligibleSlot` 判据失准）|
| A4 | 死代码清理 | `SlotLease` / `config.py` | 删 3 个死字段 + `MAIL_LIST_MIN` |

**硬凭据**：三个新守卫全部通过变异验证 —— **3/3 KILLED**，且变异后 `sha256` 还原校验通过。

**副作用（当场抓到真问题）**：预检一上线就发现配置的 4 个槽位端口**全无监听**
（`proxypool_ctl.py status` 显示"我们的实例: （无）"）——
也就是说，在那个时刻跑批会 100% 以代理连接错误收场。这正是 A2 要防的形态。

### 10.2 阶段二（补工程化）—— 5/5 完成

| # | 项 | 结果 |
|---|---|---|
| ⑤ | `pyproject.toml` | 新建：项目元数据 + ruff + pytest + dev 依赖 |
| ⑥ | 收敛 `sys.path.insert` | 22 处 → `tools/_bootstrap.py` **一处** |
| ⑦ | `tests/` + pytest | 4 个文件 / **72 个测试** |
| ⑧ | CI | 新增 `ci.yml`（lint + test×2），`secret-scan.yml` **保持独立** |
| ⑨ | 抽出 `src/redact.py` | 7 个调用点全部改完，**不留兼容壳** |

**前后对比（可复核）**：

| 指标 | 改前 | 改后 |
|---|---|---|
| `ruff check .` 告警 | **87** | **0**（`All checks passed!`）|
| pytest | 无 | **72 通过** |
| `sys.path.insert` 出现处 | 22 个文件各一份 | 1 个文件（`_bootstrap.py`）|
| CI job 数 | 1（secret-scan） | 3（+lint +test 3.11/3.13）|
| `SlotLease` 死字段 | 3 | 0 |
| 脱敏函数所在 | `config.py`（与配置无关） | `redact.py` |

**保真迁移的判据**：不只比"测试通过"，而是**变异验证** ——
故意破坏 `src/quota.py` 的 `must_expire` / scope 过滤 / compact，
原 `selftest_quota.py` 与新 `test_quota.py` **必须同时变红**：结果 3/3 同时红，**漏测 0**。
`proxypool` 侧同样验证，其中一条（`_ip_held` 负值夹 0）出现**"原绿新红"** ——
说明新测试是原脚本的**严格超集**。

### 10.3 🔴 本次踩到的新坑（两个，都属同一形态）

**坑 1：批量脚本的"标记判断"被自己的文档骗了。**
`fix_root_dup.py` 用 `if not any("from _bootstrap import ROOT" in ln for ln in lines): continue`
判断"这个文件是否已收敛"。而 `tools/_bootstrap.py` 的 **docstring 里正好写着这行用法示例** ——
于是它被判为"已收敛文件"，接着被删掉了自己的 `ROOT = Path(__file__).resolve().parents[1]`。
后果：**22 个工具的导入链全断**，报错是 `NameError: name 'ROOT' is not defined`（指向 import，不指向路径）。

> 教训：**基于文本标记的批量脚本，标记必须排除"文档里的示例"。**
> 更稳的做法是解析 AST 找真实 import 语句，而不是做子串匹配。
> 另外：我在该脚本**之前**跑过一次导入冒烟（当时全绿），之后没重跑就收了尾 ——
> **改动后必须重跑同一组验证，不能沿用改动前的结论。**

**坑 2：注释里写 `# noqa` 字面量会被 ruff 当成真实指令。**
在注释里解释"noqa 要放在哪一行"时写了 `` `# noqa` ``，ruff 报
`Invalid # noqa directive ... expected ':' followed by a comma-separated list of codes`。
与坑 1 同形：**工具无法区分"代码"与"描述代码的文本"**。

### 10.4 顺带发现（不属于迁移失真）

- **`lease.released` 是冗余守卫**：去掉 `_release_locked()` 里对它的检查后行为不变，
  因为下游 `_ip_held` 的"负值夹 0 + 归零删键"本身已幂等。保留无害（省一次锁内计算），
  但**不要以为它是唯一的幂等保障**。
- **换行符不一致（13 CRLF / 30 LF）**：`core.autocrlf=true` 且无 `.gitattributes`，
  git 存储时统一转 LF，**仓库内容本来就是一致的**，只是工作区视图不统一。
  收益低、一次性 diff 风险高（会淹没真实改动），**本次不动**。

### 10.5 阶段三（6/7 完成，仅剩 #14）

| # | 项 | 状态 | 备注 |
|---|---|------|------|
| 10 | `restore_results.py` 合并规则收进 `ledger` | ✅ 完成（**改了做法**） | 不是换成 `merge_records`，见 §10.7 |
| 11 | `tools/` 分 5 个子目录 + `probes/README.md` | ✅ 完成 | 见 §10.8 |
| 12 | `AccountRecord.error_kind` 结构化 | ✅ 完成 | 见 §10.9 |
| 13 | 抽 `QuotaGovernor` | ✅ 完成 | 差分等价测试钉住，见 §10.10 |
| 14 | 拆 `browser_login.py` → `src/browser/` | ✅ **完成**（阶段 A 文件内重构 + 阶段 B 拆包） | 955 行 → **9 文件 / 1108 行**；`_run_attempt` **268 → 70 行**；**32 个定义源码逐字节未改**；全链路 + 差分验收 ✅，见 §10.17 / §10.18 / **§10.19** / [`refactor-14-browser-split-plan.md`](refactor-14-browser-split-plan.md) |
| 15 | README 拆分到 `docs/` | 🟡 部分 | 安全约定已外置，见 §10.11；主体仍在 README |
| 16 | 池子状态持久化（`monotonic` → `time.time()`） | ✅ 完成 | 见 §10.12 |

### 10.6 已拍板：`tools/selftests/*.py` **移除**（2026-09-19 晚）

**原状态**：三个脚本（+ `_path.py`）保留未删，理由是"项目承诺 clone 下来就能跑，
pytest 需要先装 dev 依赖；自检脚本只用 stdlib"。

**老板决定**：移除，走"单一真源"。

**移除前提已满足**：迁移保真由**变异验证**证明过（§10.2）——改一处源码，
原 `selftest_*.py` 与新 `tests/test_*.py` **同时变红**，3/3 命中，漏测 0。
所以这不是"删掉一份没验证过的备份"，是"删掉一份已证明等价的重复实现"。

**移除清单**（4 文件 / 910 行）：

| 文件 | 行数 | pytest 对应 |
|---|---|---|
| `tools/selftests/selftest_merge.py` | 198 | `tests/test_ledger_merge.py` + `test_ledger_fragments.py` |
| `tools/selftests/selftest_proxypool.py` | 312 | `tests/test_proxypool.py` |
| `tools/selftests/selftest_quota.py` | 373 | `tests/test_quota.py` |
| `tools/selftests/_path.py` | 27 | —（仅为上面三个服务的 sys.path 垫片） |

**同步改的引用面**（7 处，全部落地）：

| 位置 | 改动 |
|---|---|
| `.github/workflows/ci.yml` | 删掉「零依赖自检（只用 stdlib）」step —— 它跑的就是这三个脚本 |
| `README.md` 快速开始 #8 | 三个自检命令 → 一个 `pytest` + 闸门自检 |
| `README.md` 项目结构 | 删 `selftests/` 段；子目录数 5 → **4** |
| `README.md` tests/ 段 | "CI 跑这个 + 三个零依赖自检" → "CI 只跑 pytest + ruff" |
| `README.md` 槽位池自测 | 指向 `pytest tests/test_proxypool.py` |
| `README.md` 配额验证表 | 指向 `pytest tests/test_quota.py` |
| `README.md` 台账合并节 | 指向 `pytest tests/test_ledger_{merge,fragments}.py` |

**保留不动**：`tools/gates/selftest_check_leaks.py` —— 它名字里也有 `selftest`，
但**不是重复实现**，是闸门的**变异验证**（唯一能证明"闸门真的会拦"的东西），
CI 的 secret-scan job 依赖它。`docs/security-conventions.md` 与
`docs/credential-rotation-2026-09-19.md` 里提到的是它，未改。

**回滚路径**：原目录整体移入
`.workbuddy-ai/tmp/quarantine/selftests-removed-20260919/selftests/`（**未 `rm`**），
逐字节保留；另 `git` index 中仍有 `tools/selftests/*.py`。
恢复即 `mv` 回去 + 还原 `ci.yml` 那个 step。

**副作用**：README 里"不装 pytest 也能自检"这条承诺**撤销**。
代价可接受 —— CI 本来就 `pip install pytest`，而 `tests/` 的依赖只有
stdlib + pytest（无 requests / cryptography / playwright），"clone 下来装个 pytest 就能跑"
仍然成立。

### 10.7 #10 落地记录：结论与方案原文相反

**方案 §6 的原计划**：`restore_results.py` 改用 `ledger.merge_records()`（风险标"低"）。
**实测结论**：**不能换**。换过去会让 **15 个账号丢 18 个字段**（`source` × 15、`verify` × 3）。

#### 证据链（全部可复跑）

| 步骤 | 数据 | 说明 |
|------|------|------|
| 1. 直接替换 | 旧 217 账号 / **3111** 字段；新 217 / **3093** | 差 18 字段 |
| 2. 定位到账号 | 15 个账号 `new ⊂ old`，互不包含 0 个，净增 0 个 | 纯丢失，没有换取任何东西 |
| 3. 算理论最大 | 旧算法 **0** 个账号未达理论最大；新算法 **15** 个 | 旧算法本来已达上限，是规则太严丢的 |
| 4. 定位分支 | 15 个账号全部走 `ledger` 的**降级分支**（`r_new < r_old`） | 该分支什么都不做 |
| 5. 对照实验 | A/B/C/D/E/F/G 七种候选规则 | F 达标：0 丢失 / 0 抹值 / **0 值变更** |

#### 根因：两个函数的"降级行为"必须不同

| | `merge_records`（运行期合并） | `merge_fragments`（重建台账） |
|---|---|---|
| 降级记录是什么 | **一次失败尝试**（`error` / 中间态字段） | **`export_keys` 的导出行**（`source` / `verify`） |
| 降级时该做什么 | **不动** —— 失败原因不该挂到成功账号上 | **补缺口** —— 那是账号的真实属性 |

`merge_records` 的降级门控是**承重设计**，有三处独立证据：

1. `tests/test_ledger_merge.py::test_t9c` 断言 `up9c == 0`（降级不产生任何改动）
2. `tools/run_downstream.py:269` 显式写 `out["error"] = ""`，注释写明"合并是**并集**"——
   **用空值覆盖是清空陈旧字段的正式手段**
3. `tools/run_downstream.py:326` 调用方主动交超集 `{**d, **out}`，规避"整条替换"洗字段

⇒ 所以**没有**改 `merge_records`（一行没动），而是把规则抽成 `ledger.merge_fragments()`。

#### 最终落地

| 文件 | 改动 |
|------|------|
| `src/ledger.py` | 新增 `merge_fragments()` + `_EMPTY` + `_UNSET`；`merge_records` 未动 |
| `tools/data/restore_results.py` | 删 `FIELDS`/`_RANK`/`_rank`/`_richness`/`_better`/`_fill_missing`（−45 行），改调 `merge_fragments` |
| `tests/test_ledger_fragments.py` | 新增 18 个用例（性质测试 + 对照测试 + 真实台账回归） |
| `tools/selftests/selftest_merge.py` | 新增 `[T10]` 4 项断言（28 → 32），覆盖 CI 的零依赖路径 |

**验收凭据（字节级）**：把 `git show HEAD:tools/data/restore_results.py` 取出来跑一遍，
与新版输出 `cmp` —— **两份 `results.json` 逐字节完全相同**（336560 字节，217 条）。
报告输出的唯一差异是 `--out` 文件名。

脚本已收敛为一个可复跑文件（自清理临时产物）：

```bash
python .workbuddy-ai/tmp/verify_restore_equivalence.py          # 对比 HEAD
python .workbuddy-ai/tmp/verify_restore_equivalence.py --rev HEAD~1
```

⚠ 它只在改动**未提交**时有意义 —— 提交后 `HEAD` 就是新版本，对比退化成"自己等于自己"。
长期的保证不靠这个脚本，靠 `tests/test_ledger_fragments.py` 的 18 个用例
（性质测试，钉"字段只增不减"，不钉具体输出值）。

#### 🔴 本次的新坑（第三个"工具分不清代码与描述代码的文本"）

`merge_fragments` 第一版**语义完全正确**（按 key 排序后 JSON diff 为空），但 `cmp` 报差异 ——
差在 **JSON 对象内的键顺序**。原因是实现按 `rank` 顺序写入，导致 `source` / `verify`
排到了字段最前面。`results.json` 是人会直接看的台账，键顺序乱了读起来费劲。

修法：先用一遍循环按**来源优先级**把键序固定（`out.setdefault(k, _UNSET)`），
再用第二遍按 rank 写值。哨兵用独立对象而不是 `None` —— `None` 本身是合法值。

**教训：字节级对比比语义对比更严。** 语义等价只能证明"读出来一样"，
字节等价才能顺带证明"人看到的也一样"。这类差异只有 `cmp` 抓得到。


### 10.8 #11 落地记录：`tools/` 分 5 个子目录（当晚收敛为 4）

**动机**（方案 §4.1）：28 个脚本平铺在 `tools/` 下，混了探针 / 闸门 / 自检 / 运维 /
台账五类职责，`ls tools/` 看不出任何结构。

**落地**：

| 子目录 | 内容 | 数量 |
|---|---|---|
| `probes/` | 一次性诊断探针（每个回答一个具体问题） | 13 |
| `gates/` | 泄漏闸门 + 钩子 + 闸门自检 | 3 |
| ~~`selftests/`~~ | ~~零依赖自检~~ → **当晚移除**（见 §10.6） | ~~3~~ → 0 |
| `ops/` | 运维（槽位启停 / 配置生成 / 健康检查） | 4 |
| `data/` | 台账读写（全部经 `ledger.py` 合并入口） | 4 |

`tools/` 根下只剩 `_bootstrap.py`（唯一的 `sys.path` 实现）与 `run_downstream.py`
（第二个入口，不属于任何子目录）。

> ⚠ 分 5 个子目录只维持了几个小时 —— 老板随后拍板移除 `selftests/`（重复实现），
> **当前是 4 个子目录**。§6 路线图与 §10.5 状态表里"分 5 个子目录"是当时的记录。

#### 🔴 本次踩到的坑：子目录里的垫片**不能叫 `_bootstrap.py`**

脚本移进子目录后 `sys.path[0]` 变成子目录，`from _bootstrap import ROOT` 会先命中
**同目录的同名文件**，于是 import 到自己，报：

```
ImportError: cannot import name 'ROOT' from partially initialized module
```

修法：子目录里放 `_path.py`（4 行：定位仓库根 → 把 `tools/` 与根插进 `sys.path`）。
**名字必须不同**，否则是循环导入。这条已写进 README 的「项目结构」节。

**验证凭据**：

| 检查 | 结果 |
|---|---|
| 导入链冒烟（AST 取顶层 import，用脚本所在目录当 cwd 复现 `sys.path[0]` 语义） | **34 / 34 通过**，环境缺包 0 |
| `--help` 冒烟（只挑含 argparse 的，避免误执行探针） | **20 / 20 通过** |
| README 内路径引用扫描（正则提路径 → 逐个 `exists()`） | 候选 **42 个**，失效 **0 个** |
| 14 处文档/代码里的探针路径改写（`.workbuddy-ai/tmp/probe_*.py` → `tools/probes/probe_*.py`） | 第二遍扫描 **0 处**（幂等 ✓） |

**顺带修正**：`probe_reg_interval.py` 的 docstring 与 `probes/README.md` 里
`REG_MIN_INTERVAL` 还写着 **2.5s**，实际早已落地为 **1.2s** —— 文档滞后于代码。

### 10.9 #12 落地记录：`error_kind` 结构化替代文本匹配

**动机**（方案 §3.2）：判断"这个失败是不是出口被封"靠 `"B0000" in rec.error`，
而**我们自己拼的守卫文案里也含 `B0000`**：

```
quota guard: 已确认 B0000（累计配额触顶），未发注册请求
```

文本匹配分不清"服务端返回的"与"我们引用的"。

**落地**：6 个取值 + 13 个打标点 + **唯一读点** `error_kind_of()`（同时吃对象与 dict）。

| 值 | 含义 | 判据来源 |
|---|---|---|
| `""` | 没有错误 | — |
| `"quota"` | 服务端返回 `B0000` —— 出口维度累计配额触顶 | 服务端响应 |
| `"quota_guard"` | 本地守卫主动中止，**一个请求都没发** | 本地 |
| `"rejected"` | 服务端明确拒绝（非配额） | 服务端响应 |
| `"network"` | 网络 / 超时 / HTTP 层 | 异常 |
| `"browser"` | 浏览器阶段（登录 / 建 key） | 异常 |

**读点唯一走 `error_kind_of()`**：`error_kind` 是空的旧记录（历史台账）会退化成
`status == "skipped"` → `""`，再退化成文本匹配 —— 这是**刻意的向后兼容**，
不是遗漏。

#### 🔴 打标时 `or` 不能省

```python
rec.error_kind = rec.error_kind or ERR_NETWORK   # ✓
rec.error_kind = ERR_NETWORK                     # ✗ 会把 ERR_QUOTA 冲成 ERR_NETWORK
```

通用 `except` 会接住已经打好标的记录，直接赋值会把更精确的类别**降级**。

#### 🔴 发现并拆掉一颗地雷

`settle_lease()` 用 `is_quota_block(rec.error)` 判"出口被封"。守卫文案含 `B0000`
→ 它会把**本地守卫的文案**读成"服务端确认的封禁"，进而调 `pool.report_banned()`
**误封一个健康出口**。

当前**不可达**（`quota_hit` 只在非池模式置位，而 `settle_lease` 只在池模式有 lease），
但埋着 —— 一旦将来放宽这个耦合就会击发。改用结构化字段后从根上消掉。

**测试**：`tests/test_error_kind.py`（约 28 用例）。含一条 **AST 判据**用例
（`test_no_record_error_is_judged_by_text_anymore`）钉住"不再有地方用文本匹配读
`rec.error`"—— 见 §10.13 的坑：这条最初用文本包含写，被 `settle_lease` 的
**docstring** 误伤。

### 10.10 #13 落地记录：抽 `QuotaGovernor`

**动机**（方案 §3.1）：`run_batch` 里散着 5 处配额决策的内联逻辑
（`quota_hit` / `_quota_lock` / `_quota_streak` / `_note_register_result` /
`_slot_has_quota`），读一遍要来回跳。

**落地**：收成 `src/pipeline.py::QuotaGovernor`，六个方法：

| 方法 | 对应层 | 职责 |
|---|---|---|
| `allow(count)` | ① 开跑前 | 窗口满 → 抛 `QuotaExceeded`；余量不足 → 裁剪并返回新值 |
| `check_slot(idx)` | ② 运行中 | `ThreadPoolExecutor` 的 accept 谓词（**在串行点之后**） |
| `claim_slot(scope)` | ② 运行中 | 拿到槽位租约后复查，返回跳过原因或 `""` |
| `note_result(ok, rec)` | ② 运行中 | 结果哨兵：连续 2 个配额证据 → 置位停止标志 |
| `hit()` / `streak()` | ③ 报告 | 供收尾统计读取 |

**判据一字未改** —— 这是这次重构的全部约束。

#### 差分等价测试怎么做的

把改造前从 `git show HEAD:src/pipeline.py` **逐字抄下来**的内联逻辑当参考实现
（`tests/test_quota_governor.py` 里的 `_ref_allow` / `_ref_check_slot` /
`_ref_claim_slot` / `_RefSentinel` / `_ref_settle`），对同输入逐项断言新旧一致：

| 用例 | 内容 |
|---|---|
| `ALLOW_CASES` | 8 组参数化差分（窗口满 / 余量 1/3 / 余量 0 / `ignore` / 无 pool …） |
| `test_note_result_matches_reference` | 池 / 非池两档 |
| `test_settle_lease_matches_reference` | 可达形状逐项一致 |
| `test_settle_lease_diverges_on_the_two_guard_texts` | **刻意钉住已知分歧** |
| `test_divergent_shapes_cannot_reach_settle_lease` | **AST 断言**分歧形状当前不可达 |

**为什么要"刻意钉住分歧"**：新旧在守卫文案含 `B0000` 的两种形状上**本来就不同**
（旧的会误判）。与其假装等价，不如把分歧写进用例并**另加一条断言证明这些形状
当前不可达** —— 这样将来谁放宽了耦合，测试会红。

#### 顺带把 `settle_lease` 提升到模块级

原 `run_batch` 里的 `_settle_lease` 是闭包，**没法单独做差分测试**。提升为模块级
`settle_lease(pool, lease, ok, rec)` 后可以直接喂输入。

### 10.11 #15 落地记录：README 拆分（部分）

**已完成**：

| 动作 | 说明 |
|---|---|
| 外置 `docs/security-conventions.md` | 195 行（标识分类 / 代码规范 / 占位符约定 / 目录规范 / 闸门 / 事故处置 / 轮换清单）。README 开头收成**指针块**，无信息丢失 |
| 新增 `docs/credential-rotation-2026-09-19.md` | 轮换记录独立成文 |
| README 内引用同步 | `gates/` 那行从「见『安全约定』那段」改为指向 `docs/security-conventions.md` |

**未完成**：README 主体仍是 2,100+ 行，仍是"设计文档 + 事故档案 + 操作手册"三合一。
拆它需要先定"哪一节属于哪一类"，属于**结构决策**而非机械搬运 —— 留待下一轮。

**本轮 README 的整理范围**（不拆章节，只修正与补全）：

- 【安全约定】收成指针块（见上）
- 「快速开始」加第 8 步「改代码前后（质量门）」
- 环境变量表补 `IR_PROXY_COOLDOWN_MAX` / `IR_PROXY_STATE` / `IR_PROXY_PREFLIGHT` /
  `IR_SLOT_EGRESS_IPS`
- 新增「🔴 池子状态会落盘」小节（三条设计约束）
- 「项目结构」整节重写（5 子目录 / `pyproject.toml` / `tests/` 清单 / `_path.py` 说明）
- 「输出」节：JSON 示例补 `error` / `error_kind` / `timings` / `created_at` /
  `proxy_slot`；新增字段表 + 「🔴 `error_kind`」小节
- 「三层保护」节补 `QuotaGovernor` 方法表 + 差分测试说明
- **修正两处过时断言数**：`selftest_quota.py` **46 → 60**；
  另核实 `selftest_merge.py` 32、`selftest_proxypool.py` 36（均与实测一致）

### 10.12 #16 落地记录：池子状态持久化

**动机**（方案 §2.3）：封禁退避是 `120s → 240s → … → 6h`，而服务端配额窗口是
**24h**。进程一退退避就重置回 120s —— 等于每重跑一次批量就在**同一个已被封的出口**
上重新撞一遍（一天约 720 次）。

**落地**：冷却 + 封禁次数落到 `.workbuddy-ai/state/proxypool.json`
（原子替换写；`IR_PROXY_STATE` 可覆盖）。

三条设计约束：

1. **时间用墙钟 `time.time()`，不是 `monotonic`** —— 状态要跨进程读写，而 monotonic
   的原点是进程启动时刻，两个进程之间没有可比性。
   （`acquire()` 的等待超时仍是 monotonic —— 那是进程内时长。两套钟不混算：
   `deadline` / `wait` 走 monotonic，`wake` 走墙钟并减 `time.time()`。）
2. **键是 `host:port`，不是槽位位置号** —— `slots.txt` 增删一条会让位置号整体平移，
   冷却会静默错配到别的出口头上。用 `host:port` 也顺带避免把代理账密写进文件。
3. **读不出来就降级，不抛** —— 坏掉的状态文件不该让整批跑不起来，最坏后果只是
   退避从第一档重来。

租约（`_free` / `_ip_held`）与均衡计数（`_uses`）**不落盘**。

**测试**：`tests/test_proxypool.py` 新增「状态持久化」组约 12 用例，覆盖
跨池续退避 / 过期冷却仍留封禁次数 / 键按 `host:port` / 未知槽位与坏条目忽略 /
4 种垃圾输入不抛 / **文件里不含账密** / 租约与计数不落盘 / 墙钟口径 /
过期项保存时清除 / 不留临时文件。

#### 🔴 本次修掉的一个真源码 bug

`_load_state` 里写的是 `(raw.get("slots") or {}).items()`。状态文件若被改成
`{"slots": "不是 dict"}`，字符串 `.items()` 直接 `AttributeError` ——
**"坏文件只降级不抛"这条路的全部意义所在**，第一版自己就违反了。
改成显式 `isinstance(raw_slots, dict)` 判断。

### 10.13 本轮踩到的新坑（第四个"工具分不清代码与描述代码的文本"）

| 坑 | 表现 | 修法 |
|---|---|---|
| **并行编辑同一文件会静默丢改动** | 对 `src/pipeline.py` 连发多个 Edit，**均报成功**，实际只落一处 | 改**串行逐个**编辑 + 事后 `grep -n "error_kind = "` 交叉核对（据此发现 3 处标真的丢了，补回） |
| **AST 判据优于文本包含** | `test_no_record_error_is_judged_by_text_anymore` 用 `in src`，命中了 `settle_lease` **docstring** 里描述性的 `is_quota_block(rec.error)` | 改 `ast.parse` + `ast.unparse` on args + 查 `ast.Constant` 的 `"B0000"` 字面量 |
| **AST 忘了跳 docstring** | 断言 `fn.body[0]` 是某语句，但它其实是 docstring（`Expr(Constant)`） | `_body_without_docstring(fn)` |
| **自检污染真实状态目录** | `selftest_proxypool.py` 用例间共享冷却 → 红；并写出真实 `proxypool.json`（内容含 `"B0000 测试"`） | 模块级重定向 `IR_PROXY_STATE`（**在建任何池子之前**）+ `fresh_pool()` 按用例隔离；污染文件 `mv` 到 `.workbuddy-ai/tmp/quarantine/`（不 rm） |
| **`Path.read_text()` 的换行转换** | CRLF 文件上 `s.replace("...\n", ...)` 静默失配，而**下一行**的替换成功 → 出现"用了 `fresh_pool` 但没定义" | 改文件优先用 Edit 工具，别在 bash heredoc 里嵌 Python 多行字符串替换 |
| **`f-string` 里嵌含 `"` 的字符串** | 语法错 | 提前算出来 |

### 10.14 阶段三验收凭据（全量，2026-09-19 · **移除 selftests 之前**）

| 检查 | 命令 | 结果 |
|---|---|---|
| 静态检查 | `uvx ruff check .` | **All checks passed!** |
| 行为测试 | `python -m pytest` | **160 passed** |
| ~~零依赖自检 ×3~~ | ~~`tools/selftests/selftest_{merge,quota,proxypool}.py`~~ | **已于当晚移除**（见 §10.6），当时 32 / 60 / 36 全绿 |
| 泄漏闸门 | `tools/gates/check_leaks.py` | ✅ 未发现泄漏（退出码 0，68 个文件） |
| 闸门自检（变异测试） | `tools/gates/selftest_check_leaks.py` | ✓ 全部通过 —— 既不过度拦截，也确实会拦 |
| 导入链 | `.workbuddy-ai/tmp/import_smoke.py` | **34 / 34**，环境缺包 0 |
| CLI `--help` | `.workbuddy-ai/tmp/help_smoke.py` | **20 / 20** |
| README 路径引用 | 正则提路径 → `exists()` | 候选 42，失效 **0** |
| 状态目录污染 | `ls .workbuddy-ai/state/` | 只有 `register_quota.jsonl`，**无 `proxypool.json`** |

### 10.15 #14 分析记录：`browser_login.py` 拆分方案（未动代码）

**产出**：独立文档 [`refactor-14-browser-split-plan.md`](refactor-14-browser-split-plan.md)
（含实测画像、三条硬风险、两阶段方案、验收判据、回滚）。

**取证脚本**：`.workbuddy-ai/tmp/analyze_browser_login.py`（只读，可复跑）。

**四条实测结论**（都推翻了方案 §3.1 的某个假设）：

| # | 结论 | 数据 | 对方案原文的影响 |
|---|---|---|---|
| 1 | **`_run_attempt` 268 行里注释只占 36 行（13.4%）** | code 394 / comment 147 / docstring 193 / blank 97 | "注释搬走就能瘦"**不成立** —— 它是真长 |
| 2 | **"拆完降到 60~80 行"靠拆文件拿不到** | 拆包不改变任何函数长度 | 该目标要靠**状态容器化**才达成，属行为改造 |
| 3 | **`probe_headless.py` 运行时改写 `bl.CHROME_ARGS`** | `probe_headless.py:76-88` | 拆包后兼容壳会**静默吞掉**这个 patch —— 方案没提这条 |
| 4 | **该模块零测试覆盖** | `grep -rln browser_login tests/` → 无 | 方案把风险归因于"注释是资产"，**真正来源是没回归网** |

**顺带发现**：`login()`（L801–831）与 `BrowserSession.login()`（L752–773）
是两份几乎相同的重试循环，重复约 20 行，可抽 `_retry_loop()` 消掉。

**结论**：建议**先做阶段 A（文件内重构）**，`_run_attempt` 268 → ~50 行、公共 API 零变化；
**阶段 B（拆包）缓做**，且必须先解决 `CHROME_ARGS` 注入方式。

### 10.16 移除 selftests 后的复验（2026-09-19 晚）

| 检查 | 移除前 | 移除后 | 说明 |
|---|---|---|---|
| `uvx ruff check .` | ✅ | **✅ All checks passed!** | |
| `python -m pytest` | 160 passed | **160 passed** | 测试数不变（selftests 本来就与 tests/ 重复） |
| 导入链 `import_smoke.py` | 34 / 34 | **30 / 30** | 少 4 个文件（3 selftest + 1 `_path.py`） |
| `--help` `help_smoke.py` | 20 / 20 | **20 / 20** | 少的那 3 个脚本本来就没 argparse |
| README 路径引用 | 候选 42，失效 0 | 候选 **38**，失效 **2** | 2 处是**刻意保留的历史说明**（"原 `tools/selftests/` 已移除"），非失效链接 |
| `ls .workbuddy-ai/state/` | 无 `proxypool.json` | 无 `proxypool.json` | 零污染 |
| CI `ci.yml` | 3 个 job step | 删掉「零依赖自检」step | 该 step 跑的就是这三个脚本；lint + pytest 保留 |

**回滚路径**：`.workbuddy-ai/tmp/quarantine/selftests-removed-20260919/selftests/`
（4 文件 / 910 行，逐字节保留，**未 `rm`**）。

### 10.17 #14 阶段 A 落地（2026-09-19 晚）

**做了什么**：按 §10.15 的方案执行「阶段 A 文件内重构」——
`src/browser_login.py` 由 831 行 → **955 行**，但 **`_run_attempt` 由 268 行 → 70 行**，
且**内部闭包捕获由 3 个 → 0 个**。公共 API **零变化**。

| # | 动作 | 结果 |
|---|---|---|
| 1 | `chrome_args` 注入（原方案的 P0 风险） | `_launch_kwargs(headless, chrome_args=None)`；`login()` / `BrowserSession` 都收 `chrome_args=`；`probe_headless.py` 已改用新注入面 |
| 2 | 补契约测试 | 新增 `tests/test_browser_login.py`，**39 用例**，零浏览器 |
| 3 | 抽 `_AttemptState` | 5 个共享 dict + 3 个闭包（`mark`/`ev`/`on_response`）→ 1 个类 |
| 4 | 抽 8 个 `_step_*` | 按原注释编号 `1)~8)` 与 `mark()` 埋点切（切点不是我发明的） |
| 5 | 抽 `_retry_loop` | 消掉 `login()` / `BrowserSession.login()` 的约 20 行重复 |
| 6 | 抽 `_build_result` | `captcha_stage` 形状逐字保留 |

**验收凭据**：

| 检查 | 前 | 后 |
|---|---|---|
| `uvx ruff check .` | ✅ | **✅ All checks passed!** |
| `python -m pytest` | 160 | **199 passed**（+39） |
| 导入链 | 30 / 30 | **30 / 30** |
| 泄漏闸门 | ✅ | **✅**（69 文件） |
| `py_compile src/browser_login.py` | — | **OK** |

**⚠ 未验证**：`_run_attempt` 的**实质行为**（验证码通路 / 点击轨迹 / JWT 捕获）
**没有离线验证** —— 上面的检查全是静态或契约级。最终验收必须跑一次真登录，**尚未执行**。

> ✅ **2026-09-19 晚已补做** —— 见 §10.18。

#### 🔴 执行中犯的错：给无 argparse 的探针加 `--help`，它真跑了

```
python tools/probes/probe_headless.py --help      # ← 错！它没有 argparse，直接跑完整流程
```

结果：**服务端多注册 1 个账号**（探针自己打印了 `uid=... ok=True` / `激活: True`）。

取证：本地 `register_quota.jsonl`（mtime 17:18:45）与 `results.json`（mtime 17:19:15）
**均未变**，该邮箱 `grep` 0 命中，无残留截图 → **本地账本无污染**。

**根因不是"手滑"，是两个盲区叠加**：

1. `help_smoke.py` 已经写明"只挑含 argparse 的脚本，否则 `--help` 会真执行" ——
   但我**手动**跑了探针，绕过了那个筛选器。
2. **探针直连 `sso.register()`，不经过 `pipeline`，因此不经过 `quota.record()`** ——
   本地配额账本**永远记不到探针的注册消耗**。这是**既存盲区**，非本次引入：
   `probe_register_ip.py` / `probe_quota_scope.py` 等"只打注册一枪"的探针同理，
   本地守卫会因此**低估**实际用量。

**正确做法**：跑探针前先 `grep -l argparse tools/probes/probe_*.py`，
有输出的才敢加 `--help`；或用 `help_smoke.py`（自带筛选）。

**回滚**：`.workbuddy-ai/tmp/quarantine/browser-login-stageA-20260919/browser_login.py.before`
（sha256 前 16 位 `52ac52640f11dea1`）；改写器 `apply_stage_a.py` / `apply_stage_a2.py`
（支持干跑 + 边界断言）；改动未提交，`git checkout --` 可退。

### 10.18 #14 阶段 A 真全链路验收（2026-09-19 晚）

**结论**：阶段 A **行为等价**，正向端到端全通。详细记录见
`docs/refactor-14-browser-split-plan.md` §9.7，此处只留结论与凭据索引。

**① 正向全链路** ✅ —— `tools/run_downstream.py --limit 2 --workers 1 --headless --no-write`
（20:37:41 → 20:38:28，46.0 s）：**登录 2/2 · 只读额度 2/2 · 建/复用 Key 2/2 · 真实推理 2/2**，
两个账号都 `captcha=A`（TRACELESS 自过）—— 直接证明重构后
`captcha_stage["path"]` 分类仍正确，而不只是"字段还在"。

**② 差分比对**（新旧实现跑同一账号，规范化成结构指纹后逐字段比）：

| 轮次 | 顺序 | `code_len` 新 / 旧 | 其余 18 字段 |
|---|---|---|---|
| 前向 | 新 → 旧 | 20 / **0** | 全一致 |
| 反转 `--reverse` | 旧 → 新 | 20 / 20 | 全一致 |
| **控制组** `--control` | 新 → 新 | 20 / 20 | 全一致 |

**③ `code_len` 差异定论：不是重构引入的**，三条独立证据：

1. **AST 级证明代码逐字等价**（`prove_on_response_equiv.py`）：抽 `on_response`，
   只做三类可枚举的非语义归一化（闭包容器→属性、闭包函数→方法、docstring/`-> None`
   注解/`cap = self.cap` 纯别名），归一化后 **AST 完全一致（各 4195 字符）**。
2. **该字段零生产读者**（`scan_code_field_readers.py`，AST 扫描 53 个 `.py`）：
   全仓 10 处 `.code` 访问，**0 处**基名是 `LoginResult` 型变量。
3. **机制上是竞态**：`internal/auth` 响应由 Playwright 异步回调，`_build_result()`
   收尾时同步读 `st.code`，谁先到看网络时序。已固化为测试
   `test_code_is_a_race_snapshot_taken_when_the_attempt_ends`。

⇒ `code_len` 从差分指纹**降级为参考项**：它本身不确定，不构成不等价判据。

**④ 副作用核对**（差分跑了 4 次真登录）：`results.json` mtime **17:19:15**、
`register_quota.jsonl` mtime **17:18:45** —— 均**未变**；`src/browser_login.py`
mtime **20:30:22**（早于正向验收）—— 验收期间未改。差分只登录、不消耗注册配额、
零账本写入。

**⑤ 复验全绿**：`ruff` **All checks passed!** · `pytest` **200 passed** ·
导入链 **30/30** · `--help` 冒烟 **20/20** · 泄漏闸门 ✅（69 文件）· 闸门自检 ✓。

#### 🔴 本轮踩到的坑：控制组没能复现，别把"低频不确定"说成"确定性抖动"

`code_len` 只在**前向轮**出现过一次 `0`，反转轮和控制组都没复现。
一开始我想直接判"时序抖动"—— 但**控制组（同一份代码跑两次）全一致**，
严格说只证明了「这个 `0` 不复现」，**没证明**「它必然随机」。

**正确的说法**是两条各自独立的证据合起来下结论：
- AST 证明**代码路径相同** ⇒ `0` 不可能是重构的**确定性**后果；
- 该字段**零读者** ⇒ 它**不可能**影响行为。

**两条都不成立时才需要继续跑更多轮**去统计频率。所以这里**不必**为了
"凑统计显著性"再烧登录配额 —— 判据的性质决定了下结论所需的证据量。

### 10.19 #14 阶段 B 拆包落地（2026-09-19 晚）

**结论**：`src/browser_login.py`（955 行）已拆成 `src/browser/` **9 文件 / 1108 行**，
**32 个顶层定义的源码逐字节未改**、包 docstring 逐字节未改（135 行 / 10303 字节）。
**不留兼容壳**（方案 ① 号路）—— 旧路径 `src.browser_login` 现在直接 `ImportError`，
这是刻意的：让任何漏改的引用**响亮地炸**，而不是安静地跑旧代码。

完整记录（结构表 / 依赖分层 / 凭据 / patch 失效面 / 坑清单 / 回滚）见
[`refactor-14-browser-split-plan.md`](refactor-14-browser-split-plan.md) §10，此处只留结论与索引。

#### 最终结构

| 模块 | 行数 | 职责 | 同包依赖 |
|---|---|---|---|
| `__init__.py` | 171 | 原 135 行包 docstring 逐字搬 + 显式 re-export + `__all__`(14 名) | entry/session/state/urls/constants |
| `constants.py` | 145 | 10 个可调常量（全部支持 `os.getenv` 覆盖） | — |
| `urls.py` | 23 | `build_login_url()` | — |
| `state.py` | 112 | `LoginResult` + `_AttemptState` | — |
| `behavior.py` | 110 | `_human_move` / `_idle_wait` / `_micro_move` / `_warmup_mouse` | constants |
| `captcha.py` | 56 | `_click_checkbox` / `_has_slider` | behavior |
| `attempt.py` | 299 | 8 个 `_step_*` + `_build_result` + `_run_attempt` | behavior/captcha/constants/state/urls |
| `session.py` | 136 | `_launch_kwargs` + `_retry_loop` + `BrowserSession` | attempt/constants/state |
| `entry.py` | 56 | `login()` | attempt/session/state |

依赖图由 AST 算出（`analyze_stage_b_deps.py`）：**无环、无同层互调**；入度前三
`_AttemptState`(9) / `LoginResult`(5) / `_micro_move`(3)。

#### 与方案原文的三点差异

1. **新增 `urls.py` 叶子模块** —— 方案把 `build_login_url` 放在 `entry.py`，
   但 `attempt._step_open_form()` 要调它，而 `entry.login()` 要调 `attempt._run_attempt`
   ⇒ 成环 `attempt ↔ entry`。**被依赖者必须落在调用者同层或更低层**，故单独成叶子。
2. **新增 `constants.py` 叶子** —— 方案未细化，实际 10 个常量被 4 个模块共用，独立成叶子最干净。
3. **漏了第 10 处引用面 `pyproject.toml`** —— 见下。

#### 引用面（10 处，方案只列了 9 处）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `src/pipeline.py` L564 | `from .browser_login import login` → `from .browser import login` |
| 2 | `src/pipeline.py` L772 | `from .browser_login import BrowserSession` → `from .browser import BrowserSession` |
| 3 | `tools/run_downstream.py` L107 | 同上 |
| 4 | `tools/probes/probe_captcha_timing.py` | `from src import browser` + 5 处 `browser_login.` → `browser.` |
| 5 | `tools/probes/probe_login_timing.py` | 同上 + 3 处改名 |
| 6 | `tools/probes/probe_headless.py` L75 | `import src.browser as bl`（注释更新为"旧写法已失效"） |
| 7 | `tools/probes/probe_login_only.py` L112 | 改 `src.browser` |
| 8 | `tools/probes/probe_login_route.py` L29 | 改 `src.browser` |
| 9 | `tests/test_browser_login.py` | 导入头 + 2 处 patch 目标重定向 |
| **10** | **`pyproject.toml`** | **`packages = ["src", "src.browser"]`** |

🔴 第 10 处**方案漏了**：`packages` 是**显式列表**，setuptools 不会自动发现子包 ——
漏加就会让构建出的 wheel **缺整个子包且不报错**。本仓库不用 `git ls-files` 收集发布包，
所以"新包必须先 commit"那条通用建议在此不适用，但这条配置坑更隐蔽。

#### 验收凭据

| 判据 | 结果 |
|---|---|
| 逐定义源码字节比对 | **32/32 一致** |
| 包 docstring | **135 行 / 10303 字节一致** |
| 原文件行归属 | **955 = 144(preamble) + 643 + 168**，无遗漏无重叠 |
| `ruff` | All checks passed |
| `pytest` | **201 passed**（基线 200 + 1 条反向守卫） |
| 导入链 | **30/30** |
| `--help` 冒烟 | **20/20** |
| 泄漏闸门 | ✅ **78 文件 = 69 + 9** |
| 闸门自检 | ✓ |
| 8 个子模块单独 import | 全部无循环 |
| 真全链路 | 登录 2/2 · 只读额度 2/2 · 建/复用 Key 2/2 · 真实推理 2/2（wall=92.1 s） |
| 差分（新 vs 旧） | **18/18 结构字段一致**（新 19.6 s / 旧 19.4 s） |

**🔴 60 秒登录的虚惊（已查清，与拆分无关）**：真全链路里账号 2 登录 60023 ms，
而阶段 A 基线是 18836 ms，60 s 恰好是 `MICRO_BUDGET_S=45` 兜底压到的上限，一度怀疑拆分
让兜底失效。取证两条：① 差分里同一时刻跑新旧代码，分别 **19.6 s / 19.4 s**；
② `cs_mouse_budget_exhausted` **两次都是 `False`** ⇒ 45 s 兜底**根本没触发**。
结论：**环境抖动**。

#### 🔴 阶段 B 的六个坑（按危险度排序）

1. **CRLF 双重转换 → `\r\r\n`**（最严重）：生成器保留原 CRLF 的块，落盘前又做全局
   `\n → \r\n`，每个 `\r\n` 变成 `\r\r\n`，被 `splitlines()` 解析成**两个**换行 ⇒
   产物每行之间凭空多一个空行、行数翻倍（`_human_move` 31 行变 61 行，24/32 个定义不一致）。
   **且骗得过常规自检**：`count(b'\r\n')` 仍是每行 1、裸 LF 仍是 0，必须**单独查 `b'\r\r\n'`**。
   修法：读取时统一归一化成 LF，落盘前只做一次转换。
2. **`ast` 的 `lineno` 不含装饰器**：`LoginResult` 的 `@dataclass` 在 265 行、`class` 在 266 行，
   按 `lineno..end_lineno` 切片会漏掉装饰器 → 语法错。必须
   `min([node.lineno] + [d.lineno for d in decorator_list])`。
3. **patch 静默失效**：`from .constants import CHROME_ARGS` 在 `session.py` 绑一份副本。
   patch `session.CHROME_ARGS` **生效**，patch `src.browser.CHROME_ARGS`（包级）**静默失效**
   —— 三者初始指向同一 list，重新赋值只改被赋的那个命名空间。已固化成**两条**测试
   （一正一反）钉死，`probe_headless.py` 的注释也同步更新。
4. **ruff isort 空行规则取决于首个定义种类**：`import X` 后到**赋值语句**要 **1** 空行，
   到 **`def`/`class`** 要 **2** 空行（E302）。`constants.py` 首个是 `MICRO_MOVE = os.getenv(...)`
   ⇒ 要 1 行。**副坑**：第一次用 `--isolated` 做最小复现，它**忽略 `pyproject.toml`**，
   结论完全不可用 —— 做工具行为实验必须确认用的是项目配置。
5. **生成器的三处低级错**：`home` 用了文件名而非模块名（生成 `from constants.py import X`，
   语法能过、导入必炸）；`all_mod_names` 漏了 import 名（产物缺 `dataclass`/`time`/`config`）；
   模块 docstring 忘了补 `"""` 定界符（`SyntaxError: invalid character '：'`）。
   根因都是**生成器没做全覆盖断言** —— 补上"每行必须被恰好一个目的地认领"后全部暴露。
6. **生成器读不到原文件**：`rm -rf src/browser` 后又发现原文件已移走 → 短暂处于"两边都没有"。
   修法：加 `if not SRC.exists(): SRC = BACKUP` 回退，使生成器**可复跑**而非一次性。

#### 回滚

| 项 | 路径 / 值 |
|---|---|
| 拆分前原文（安全点） | `.workbuddy-ai/tmp/quarantine/browser-split-stageB-20260919/browser_login.py.before` |
| sha256 前 16 位 | `b1dbcdf2bce70bc9`（955 行全 CRLF，50890 字节） |
| 生成器（可复跑） | `.workbuddy-ai/tmp/gen_stage_b_split.py`（支持干跑；原文件移走后自动回退读备份） |
| 验证器 | `.workbuddy-ai/tmp/verify_stage_b_split.py` |
| 差分器 | `.workbuddy-ai/tmp/verify_stage_b_equivalence.py`（`--reverse` 可反转） |
| 依赖分析 | `.workbuddy-ai/tmp/analyze_stage_b_deps.py` |
| patch 面扫描 | `.workbuddy-ai/tmp/scan_patch_surface.py` |

⚠ 因为**不留兼容壳**，回滚**不是"改一行 import"** —— 要同时恢复 10 处引用面：

```bash
git checkout -- src/ tools/ tests/ README.md pyproject.toml
cp .workbuddy-ai/tmp/quarantine/browser-split-stageB-20260919/browser_login.py.before \
   src/browser_login.py
rm -rf src/browser/
```

改动**未提交**，`git checkout --` 可退。

