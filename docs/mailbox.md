# 邮箱源：CF Worker / IMAP / chatai / Remail

> 📄 由 [`protocol.md`](protocol.md) 按主题拆出（2026-10-06）。

临时邮箱是**可插拔**的（`IR_MAILBOX_KIND`），四套源的实测、坑与已知限额都在这里：
CF Worker（D1 读限额）/ IMAP / chatai 读信页（安全会话）/ Remail（按地址回捞订单）。

## CF Worker 临时邮箱

```
POST /api/mailboxes        {"domain": "<your-mail-domain>", "count": N} -> {"emails": [...]}
GET  /admin/all?limit=N    邮件列表（含 extracted_json 已提取的链接）
GET  /admin/msg?id=&email= 单封详情
GET  /health               {"ok":..., "database":..., "domains":..., "storage":...}
```

鉴权头 `X-Admin-Token` 与 `Authorization: Bearer` 都支持，两个都带最稳。
可用域名由 Worker 侧配置决定，`GET /health` 返回的 `domains` 字段会列出来。

`received_at` 是**毫秒** unix 时间戳（形如 `1789449135216`），不是秒。

### 🔴 `/admin/all` 的两个实测特性（决定了轮询怎么写）

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

### 🔴 2026-09-18 起 `/admin/all` 间歇性挂掉（Cloudflare Error 1101）

跑槽位池的第一次真实批量时，**注册 4/4 全成功、激活 4/4 全失败**：

```
[1/4] registered: uid=496100438 user=lz535654
[pool] 槽位 1 短冷却 20s（activate: 500 Server Error ... for url: https://temp-email-wo）
```

注意这个 `activate:` 前缀**是误导的** —— 报错发生在 `stage_register` 的
激活 try 块里，但真正 500 的是**收信轮询**（`GET /admin/all`），不是激活接口。
这是 `src/pipeline.py` 里那个大 try 块把所有异常都标成 `activate:` 的后果。

**根因**（直接打 Worker 确认）：

```
GET /admin/all?limit=5 -> 500
{"type": ".../error-1101/", "title": "Error 1101: Worker threw exception",
 "error_code": 1101, "error_name": "worker_threw_exception"}
```

`Error 1101` = **Worker 脚本抛了未捕获异常**，不是我们的请求有问题。
实测同一个请求连打 25 次只成功 **1 次（≈4%）**，而且这个成功率还在往下走
（后来 180 次全 500）。`/health` 和 `/` 都正常 → 挂的只是 `/admin/all` 这条查询。

**判据：这是"读不出来"，不是"邮件没到"**

- `/health` → 200；`GET /` → 200；`POST /api/mailboxes` → 200（建邮箱正常）
- `GET /admin/all` → 500
- 部署版本的 `email` 过滤参数**被忽略**（要 `oai-b4a67309…`，返回的却是
  `oai-6650473634a64d51…`）→ 每次都在跑**不带 WHERE 的全表 `SELECT *`**，
  而 `raw_text` / `raw_html` 单列上限 1.5MB（`MAX_RAW_LENGTH = 1_500_000`）
- → 全表扫描 + 排序 + 搬运大字段，超出 D1/Worker 资源上限，偶发挤过去

另一处本地源码是**更新的一版**，
`handleAdminAll` 已经支持 `?email=` 过滤、`allMessages` 也改成了带 WHERE 的分支 ——
但**部署的还是旧版**（旧版忽略 `email`）。

**本项目的处置（已做）**

`tempmail.wait_for_mail` 原来一拿到 500 就 `raise_for_status()` → **第一枪就把
一个已经注册成功的账号判死**。现在改成：

- **5xx 重试**（轻微退避，上限 2s），4xx 立刻失败
- 轮询结束仍未拿到时，`mail.last_error` 区分两种情况，
  由 `stage_register` 原样报出来：
  `邮箱 Worker 持续 5xx（45 次，最近 HTTP 500）—— 不是邮件没到，是读不出来`

语义上：**5xx = "服务端现在读不出来"，不是"这封邮件不存在"**。
轮询本来就是在等，多等几次的代价远小于丢掉一个已注册账号。
（4xx 才是我们的问题：401 凭据错、404 路径错，必须立刻失败。）

**🔴 根因已被印证：D1 读取超限额（2026-09-18 下午）**

维护者确认：**当天 D1 数据库读取超限额了**。这与上面的推断完全一致 ——
无 WHERE 的全表 `SELECT *`（还要 `ORDER BY received_at DESC` 排序），
单列上限 1.5MB，**每打一次 `/admin/all` 就是一次全表读**。
D1 免费版是"每天 500 万行读取"量级的硬限额，被打满后查询直接抛异常 → `1101`。

**"修复"之后的复核（同日下午 16:10）**

| 探测 | 结果 |
|------|------|
| `GET /health` | 200 ✅ |
| `GET /admin/all?limit=5` | **200**，16900B，0.57s（5 封） |
| `GET /admin/all?limit=50` | **500** |
| `GET /admin/all?limit=1 / 3 / 5 / 8 / 10 / 12 / 15 / 20 / 30` | **全部 500** |
| `GET /admin/all?email=<不存在>&limit=5 / 1` | **全部 500** |
| 连续 5 次轮询（实测计数器） | 成功 1 / 5xx 4 → **成功率 ≈ 20%** |

**三条结论**：

1. **修复不彻底**。成功率从上午的 4%（1/25）升到 20%，但远未恢复。
2. **和 `limit` 大小无关**。`limit=1` 和 `limit=50` 一样会 500 ——
   说明失败不是"返回体太大"，而是**查询本身就重**（无 WHERE ⇒ 全表扫）。
   失败的请求 **0.27s 就返回**（D1 直接拒绝），成功的 0.57s。
3. **带 `email` 过滤那条路径仍然是坏的**（一律 500）。注意这与上午的观测
   矛盾（上午带 `email` 有时 200，只是返回的是**别人**的邮件 ⇒ 参数被忽略）。
   两者合起来说明：`email` 参数确实被读了，但**那条分支的查询更重**
   （`WHERE to_address = ?` 没有索引 ⇒ 依然全表扫）。
   → 所以**"改用 email 过滤来省 D1 读取"这条路目前走不通**，
   `wait_for_mail` 保持"无过滤 + 自适应窗口"是正确选择。

**🔴 我们很可能就是元凶之一**

"注册一个账号 = 轮询 N 次 `/admin/all` = N 次全表读"。故障期这个 N 会暴涨
（实测一个账号 45 次重试），4 个账号并行就是 ~180 次全表读。

所以现在**给 `wait_for_mail` 加了计数器**，把这个数变成可对账的凭据：

```
rec.timings["register_detail"]["mail_polls"]   # 打了几次 /admin/all
rec.timings["register_detail"]["mail_5xx"]     # 其中几次是 5xx 重试
```

`run.py` 的「注册内部阶段」报告会打印：

```
  mailbox            0.44s
  username           4.55s
  gate_wait          0.85s
  register_call      1.14s
  收信轮询              5 次 /admin/all，其中 5xx 重试 4 次
```

> ⚠ `register_detail` 里**其它键都是毫秒**，这两个是**计数**。
> 混进 `/1000` 那个循环会打印成 `0.05s`，看着像个耗时 ——
> `run.py` 里用 `COUNT_KEYS` 显式排除了。

**⚠ 在 Worker 真正修好之前，不要跑批量注册。**
每次注册都在花 D1 读取，而限额是**全站共享**的（这是个公开的临时邮箱服务），
打满之后连你自己也读不出来 —— 等于自己把路堵死。

**真正的修法（都在 Worker 侧）**

1. **给 D1 加索引**：`CREATE INDEX ON emails(to_address, received_at DESC)` +
   `CREATE INDEX ON emails(received_at DESC)` —— 让两个查询都走索引，
   从"全表读"变成"读几行"
2. **重新部署新版 Worker**（本地源码已支持 `?email=` 过滤）
3. **别在列表接口里 `SELECT *`**：`raw_text` / `raw_html` 单列上限 1.5MB，
   列表根本不需要它们（`rowToMessage` 默认 `includeBody=false` 本来也不返回），
   但 SQL 已经把大字段读出来了 —— 改成显式列清单
4. **降低轮询频率**，或让客户端只在必要时才放大窗口

**还没解决的（需要人介入）**

Worker 现在 100% 打不通，激活拿不到邮件。两条路都**在另一个工程里**：

1. **重新部署新版 Worker**（`npx wrangler login` + `npx wrangler deploy`）——
   新版带 `?email=` 过滤，查询从"全表 `SELECT *`"降到"按收件人取几行"
2. **给 D1 加索引 / 清历史**（`npx wrangler d1 execute temp-email-db`）——
   `ORDER BY received_at DESC` 与 `WHERE to_address = ?` 各需要一个索引；
   库是公开服务共用的，7 天保留期靠 cron 清，量仍然很大

本机**没有 wrangler、也没有 Cloudflare 凭据**（`~/.wrangler` 不存在，
无 `CLOUDFLARE_API_TOKEN`），所以这一步没法自动做。
D1 database_id 在 `wrangler.toml` 的 `database_id` 字段里（也可用 `wrangler d1 list` 查）。

**救已注册但没激活的账号**：`tools/data/recover_activation.py`

```bash
# `--from` 要传**台账读源**（`runs/` 里最新的全量快照）。取路径：
python -c "from src import ledger; print(ledger.ledger_path())"

# 列出候选（判据：stages.register == "ok" 且 activate 未成功）
python tools/data/recover_activation.py --from <台账读源> --dry-run

# 真补激活，并把结果并集写回台账
python tools/data/recover_activation.py --from <台账读源> --write
```

⚠ **不要**传 `ledger/latest.json` —— 它只含最近一批那几十条，历史账号不在
里面，候选会少一个数量级，而且**不报错**。

判据卡在 `stages.register == "ok"` 上，**不是**只看 `status == "failed"` ——
注册本身失败的账号（`B0000` 之类）服务端根本没这个账号，补激活无从谈起。

### 🔴 注册阶段的真实瓶颈：收信轮询，不是注册接口

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

### 🔴 站点对收信域名走**黑名单 + 配额**，不是严格白名单（2026-09-23 实测）

注册接口 `register/byEmail` 会校验**邮箱域名**，不在名单里的一律拒：

```json
{"traceId":"…","msgCode":"A0232","msg":"该邮箱域名暂不支持注册，请更换邮箱","success":false}
```

判据 = 该响应**带 `traceId`** ⇒ 是**应用层**判定，与客户端形态无关。
所以换出口 IP、换浏览器链路**都没用** —— 只能换域名。

三臂控制变量实测（探针 `tools/probes/probe_domain_gate.py`，
**全用合成地址、不消耗真实邮箱**）：

| 域名 | 结果 |
|---|---|
| 本项目 Worker 域名（`IR_WORKER_DOMAIN` 及其 7 个子域） | ❌ `A0232` |
| `icloud.com` | ❌ `A0232` |
| `gmail.com` | ✅ 通过 |
| `gmail.com` + `+tag` 别名 | ✅ 通过 |
| `outlook.com` | ✅ 通过 |
| `qq.com` | ✅ 通过 |

补测一个**供应商给的冷门域名**（非本项目自有、此前没人用过）⇒ **通过**
（注册成功，`ssoUid` 已记入台账）。所以它**也不是**"只放行主流服务商"的白名单。

三条推论：

1. **被拒的是域名，不是 `+别名` 格式** —— 无别名的 `@icloud.com` 照样被拒，
   而带 `+tag` 的 `@gmail.com` 通过。
2. **不是"封所有真实邮箱"** —— 主流服务商能过。
3. **也不是"只放行主流服务商"** —— 冷门域名同样能过。
   结合文案里的「**暂**不支持」与 09-22 单域名跑出 710 条成功，
   更可能是**黑名单 + 域名配额** ⇒ 策略是**轮换新鲜域名**，
   而不是去找"哪个大厂域名能用"。
   选发信域名前**先用探针验一遍**，别猜。

⚠️ 两个会造出**假结论**的坑（都实测踩过）：

- **无 `traceId` 的 429 是网关层限流，不是域名判定。**
  `{"error":"Too many request"}`（无 `traceId`、`ct=UTF-8`）必须退避重试后重读 ——
  第一版就是因为把 429 当结论，差点得出「gmail 别名被拒」的假结论。
  写接口限流很凶：正对照**连撞 2 次 429** 才通。
- **挑战页也不是结论。** 写接口有阿里云 WAF 的 JS 挑战（`200 + text/html`），
  不解盾就会落到 `r.json()` 抛 `JSONDecodeError`，把"被盾拦了"误读成
  "邮箱 / 网络错误"。解法见 `tools/probes/probe_domain_gate.py` 的 `solve_waf()`。

#### 补充：Apple 三个别名域名里，只有 `icloud.com` 被拒（同日实测）

`@icloud.com` / `@me.com` / `@mac.com` 是**同一个 iCloud 收件箱**的三个别名。
逐个测（同一正对照、同一出口）：

| 域名 | 结果 |
|---|---|
| `icloud.com` | ❌ `A0232` |
| `me.com` | ✅ 通过（注册成功） |
| `mac.com` | ✅ 通过（注册成功） |

⇒ 黑名单是按**域名字符串**写的，没有覆盖 Apple 的另外两个别名。

**但这条路走不通。** 判据是两半（缺一半就会得出相反结论）：

1. 拿真实 iCloud 账号把域名换成 `me.com` / `mac.com` 去注册（域名门确实放行），
   再轮询供应商托管收信页 **19 次 / 150 s** —— 页面始终 `0 封`。
2. **正对照**：13 个收信页里有 **1 个已有邮件**（ChatGPT 验证码，页面 11 KB）
   ⇒ 这个读信页**确实能显示邮件**，所以上面那个否定**有效**，不是"页面不工作"。
3. 解码页面里 Cloudflare 的 `data-cfemail` 后确认：**每个 token 与地址 1:1 绑定**。

⇒ 两种解释指向同一结论：**要么账号没有这两个别名，要么读信页只列
`To` == 绑定地址的邮件**。无论哪种，现有供应商读信页都**服务不了 `me.com` 注册**。

⇒ 要这条路的唯一办法：**让供应商直接发 `@me.com` / `@mac.com` 地址 + 对应 token**
（域名门这一关已证明放行）。

⚠️ **方法论**：这一步差点被读成"`me.com` 收不到信 ⇒ 别名不存在"。
但"收不到"有两个完全不同的原因 —— **邮件没送到** vs **读信页根本不工作**。
没有那个"1/13 有邮件"的正对照，这个否定结论**不成立**。
⇒ 任何"没收到 / 没变化"的否定结论，都要先给**观测工具本身**找一个正对照。

### 🔴 两道坎互相独立，没有任何邮箱源同时过得了 ⇒ 邮箱源做成可插拔（2026-09-23）

把上面那条（域名门）与「能不能**可编程读信**」放一起看，结论很别扭：

| 邮箱源 | 过域名门 | 可编程读信 |
|---|---|---|
| 本项目 CF Worker 自有域名 | ❌ `A0232` | ✅ Worker 自带 `/api/inbox` |
| `gmail.com` / `outlook.com` / `qq.com` | ✅ | ❌ 需要 app password / OAuth / 授权码 |
| 供应商的 Google Workspace 定制域名 | ✅ | ❌ 同上（但凭据**已验证有效**） |
| 供应商的 iCloud 托管收信页 | ❌ | ✅（只能抓 HTML）—— 但用不上 |
| **供应商 outlook 账号池 + chatai 读信页** | ✅ | ✅ **两道都过**（见下节） |

⇒ **换域名解决不了读信，换读信方案解决不了域名。** 于是把邮箱源做成可插拔
（`src/mailbox.py`），让"换源"变成一个**配置动作**，而不是一次重构。

**契约**（`MailboxSource`，`typing.Protocol` —— 结构性契约，无继承关系）：

```
create_mailbox(domain=None, count=1) -> list[str]
wait_for_mail(address, sender_contains="openxlab", ...) -> Mail | None
wait_for_activation_link(address, **kw) -> str | None
last_error: str / last_polls: int / last_http_errors: int
```

`stage_register` 本来就是鸭子类型（`tests/test_error_kind.py` 传的是 `FakeMail()`），
所以这次改造**没动它一行业务逻辑**，只换了构造点。

**开关**：`IR_MAILBOX_KIND`（`worker` 默认 / `imap` / `chatai`）。默认值刻意保持 `worker`
⇒ 与引入开关之前**行为逐字相同**（`config.validate()` 的 worker 分支一字未改，
由 `tests/test_mailbox.py::test_validate_worker_branch_is_unchanged` 钉住）。

⚠️ **IMAP 那条路现在的状态**（两个实测结论，决定它还不能直接投产）：

1. **供应商给的密码不是 Google app password。** app password 固定为
   **16 位纯小写字母**；文件里的是 12/16 位、含大小写 + 数字 + 符号的常规密码。
   IMAP 的登录失败文案能把两种情况分开：
   - `[ALERT] Invalid credentials` ⇒ **密码是错的**
   - `[ALERT] Application-specific password required` ⇒ **密码是对的**，
     只是账号开了两步验证，必须改用 app password

   ⇒ 缺口只剩"拿到 app password"，**不是死路**。
2. **定制域名邮箱的读信 == 读 Gmail。** 该域名 MX 指向 `smtp.google.com`
   且**无 A 记录** —— 所以对它做 HTTPS 探测全是 `SSLError`，那不是网络故障，
   是这个域名本来就不提供 web 服务。同一个认证坎。

⚠️ **`issubclass()` 不能用来检查这个契约**：`MailboxSource` 含数据成员
（`last_error` 等），`issubclass` 会直接抛
`TypeError: Protocols with non-method members don't support issubclass()`。
契约断言只能用 `isinstance`（Python 3.13 实测）。

⚠️ **`ImapMailbox.create_mailbox()` 不是"新建邮箱"**，是从凭据池里**领一个**
未用过的地址并落盘去重（状态在 `.workbuddy-ai/state/imap_used.json`，
刻意不写在凭据文件旁边 —— 那通常在用户的下载目录里）。
方法名沿用只是为了让 `stage_register` 一行都不用改。

### chatai.codes 读信页：第一条**两道都过**的源（2026-09-24 实测）

供应商直发的 outlook 账号池（每行 `邮箱----密码----clientId----refreshToken`）
配一个独立读信页。它**同时**过得了域名门和读信坎，端到端实测：

```
① 正对照   本项目 Worker 域名（IR_WORKER_DOMAIN）  ❌ A0232         ← 实验有效
② 指定     供应商给的 @outlook.com 地址            ✅ 注册成功（ssoUid 已记台账）
③ 拉信     no-reply@dm.openxlab.org.cn             ✅ subject=【OpenXLab】注册激活
                                                     date=2026-09-23T18:09:37Z
④ 激活     register/active → True                  ✅ 端到端打通
```

⇒ 这是本项目**第一条**「域名门 + 可编程读信」两样都过的邮箱源。
代价在账号侧：池子会烂（见下「坑 4」）。

#### 请求体加密信封（从前端 bundle 逆向）

读信页**不接受明文请求体**。每次 POST 包一层：

```
POST /api/security-session  → {sessionId, sessionToken, sessionKey(base64url), expiresAt}
key = base64url_decode(sessionKey)                      # 32 字节
iv  = 随机 12 字节
ct  = AES-GCM(key).encrypt(iv, JSON(payload), None)
sig = HMAC-SHA256(key, f"{sessionId}.{nonce}.{timestamp}.{iv}.{ciphertext}")
body = {secure, sessionId, sessionToken, nonce, timestamp, iv, ciphertext, signature}
```

四条实现细节，每条都能把整条链路打死：

1. **`ensure_ascii=False` 必须开** —— 前端用的是 `JSON.stringify`，中文不转义。
   转义后字节不同 ⇒ HMAC 对不上 ⇒ 服务端直接 401。
2. **会话要缓存** —— 前端 `getApiSecuritySession()` 缓存到 `expiresAtMs - 60s`。
   每轮询都重建会让请求数翻倍。
3. **401/403 只在 `code == SECURITY_ENVELOPE_INVALID`（或文案指向信封）时才重建会话重试。**
   前端注释原文：「业务认证失败不能靠重建安全封包修复，重试反而会重复登录邮箱。」
4. `iv` 必须是 **12 字节**（AES-GCM 的标准 nonce 长度）。

#### 🔴 坑 1：业务失败也是 **HTTP 500**，不是 200

实测原始响应（`curl`，非推测）：

```
HTTP 500  content-type: application/json
{"success":false,"code":"TOKEN_EXPIRED_OR_REVOKED",
 "error":"刷新令牌无效或已过期，请重新获取 refresh_token",
 "detail":"Token 刷新失败: invalid_grant - AADSTS70000: The user could not be
           authenticated as the grant is expired."}
```

⇒ **判据顺序必须是「先解析 body、再看状态码」。** 先 `raise_for_status()`
会把 body 直接吞掉，于是「账号失效」被误判成「服务端故障」——
探活永远拿不到「死」，最后卡在「读信页不可达」上（实测踩过）。

⚠️ **这条差点被记错**：早先的批量脚本没调 `raise_for_status()`，
`r.json()` 照样把 500 的 body 解析成功了，于是在笔记里把状态码写成了 `200`。
**「能解析出 JSON」≠「状态码是 2xx」** —— 要判状态码就显式读它，
别从「解析成功」反推。同类坑见 §「`tempmail.wait_for_mail` 一拿到 500 就
raise_for_status」那节，方向正好相反：那边是**该重试**，这边是**该读 body**。

#### 🔴 坑 2：`fetch-imap` 是死路（恒 501）

```
HTTP 501  {"success":false,"protocol":"imap","code":"IMAP_REQUIRES_CONTAINER",
 "error":"Cloudflare Workers 免费运行时不支持当前 IMAP TCP/TLS 实现；
          请使用 Graph 或开通 Workers Paid 后部署 Containers 版"}
```

⇒ **不要做 graph → imap 的回退**。前端会回退，是因为它自己部署的 Worker 可能
开了 Containers；我们连的这个部署没开。留着回退只会让每次失败多打一枪，
并把真因（graph 侧的业务码）冲淡成一条「IMAP 不可用」。

#### 🔴 坑 3：`sender` / `keyword` 参数服务端不做模糊匹配

实测 `sender="openxlab"` 直接返回 **0 封**，而同一时刻不带过滤能拉到 10 封
（其中就有 openxlab 的）。⇒ **过滤一律在本地做**，这两个参数固定传空串。

#### 🔴 坑 4：账号池会烂，必须「探活后再领用」

供应商这一批 **31 条里 30 条的 refreshToken 已失效**（同一个
`TOKEN_EXPIRED_OR_REVOKED`）。不探活就领，等于把 30 个必死的账号挨个喂给
`stage_register` —— 而注册是**有副作用**的（站点侧会建号），白跑不是零成本。

⇒ `ChataiMailbox.create_mailbox()` 先探活，三态处置：

| 探活结果 | 处置 |
|---|---|
| 拉到邮件 | 活 ⇒ 领用 |
| 明确的失效信号（`_DEAD_CODES` / `_DEAD_PAT`） | 死 ⇒ 写进状态文件的 `dead`，下次直接跳过 |
| 认不出来的失败（网络 / 5xx / 未知业务码） | **未知** ⇒ 既不领用也不标死 |

🔴 **「证据确凿才标死」是刻意的**：一条过宽的判据（比如把任何 `AADSTS\d+`
都当失效）会在**应用级**配置错误时把整池账号一次清空。所以 `_DEAD_PAT`
里只认 `invalid_grant` / `grant is expired` / `token…expired` / `revoked`
这类「凭据本身失效」的措辞。

另有一道闸：连续 `_MAX_UNKNOWN`（3）次「结果未知」就判定**读信页整体不可达**
并快速失败，不把整池探完 —— 否则一次网络抖动会在日志里留下
「整池都不可用」的假象，而真相是网络不通。

状态文件 `.workbuddy-ai/state/chatai_used.json`，按账号文件路径分桶：

```json
{"<账号文件路径>": {"used": ["<地址>"], "dead": ["<地址>"]}}
```

⚠️ 删掉它 = 下一轮把整池死号重探一遍（实测 31 条约 1 分钟）。
⚠️ 它与「已领用」分开记：`used` 是**业务事实**（这个地址不能再发），
`dead` 是**负面缓存**（重探一次就能重建）。把死号混进 `used` 会让
「重置」这个动作变得没法做。


