# docs/ 索引

本目录装两类东西，**性质完全不同，别混着看**：

- **现行文档** —— 会被持续更新，改代码前该读的就是它们。
- **带日期的历史留档**（`*-YYYY-MM-DD.md`）—— 冻结，只作证据。看它们是为了追
  「当时为什么这么决定」，**不是为了照做**。

---

## 现行文档

| 文件 | 讲什么 | 什么时候看 |
|---|---|---|
| [`protocol.md`](protocol.md) | **协议核心**：密码加密 / 人机验证与两条通路 / 浏览器反检测（最小化注入）/ 写接口 WAF 挑战与**解盾复用** / discovery 鉴权与 JWT / 免费额度结构（双层滚动窗口 + 按 token 计费）/ key 创建与传播延迟 / 风控与频率 | 动注册·登录·建 key·额度相关代码之前 |
| [`mailbox.md`](mailbox.md) | **邮箱源**（可插拔）：CF Worker（D1 读限额）/ IMAP / chatai 读信页 / Remail —— 各源的实测、坑与已知限额 | 换邮箱源、排查「收不到激活邮件」时 |
| [`registration-limits.md`](registration-limits.md) | **注册链路：限流·配额·吞吐·耗时（实测）**：`429` 边界 / `workers` 边界 / `B0000` 累计配额 / WAF 解盾成本 / 实测耗时 | 调 `workers` / `REG_MIN_INTERVAL` / `POST_ATTEMPTS` / 配额参数**之前** |
| [`security-conventions.md`](security-conventions.md) | **公开仓库的安全与脱敏规范**：什么能进仓库、不能的放哪、怎么自动保证（泄漏闸门）；§8 另含**测试约定**（断言契约而非宿主环境） | 任何要往仓库写配置 / 凭据 / 路径 / 主机标识的时候；写新用例、碰到跨平台差异的时候 |
| [`audit-2026-09-20.md`](audit-2026-09-20.md) | **架构与工程化审计**（只读扫描）+ 后续各批次（B1–B8）的执行记录 | 想知道「为什么现在是这个结构」、或准备再审计一轮之前（§5 已把"不是问题"的项写出来了，别重复调查） |

## 带日期的历史留档（冻结）

| 文件 | 性质 | 状态 |
|---|---|---|
| [`refactor-plan-2026-09-19.md`](refactor-plan-2026-09-19.md) | 2026-09-19 重构总方案：阶段一（止血）/ 二（补工程化）/ 三（结构治理），共 16 项。§10 是**逐项落地记录** | 阶段一 **4/4** ✅、阶段二 **5/5** ✅、阶段三 **6/7** ✅（`#15` README 拆分当时只做了部分） |
| [`refactor-14-browser-split-plan.md`](refactor-14-browser-split-plan.md) | 上述方案 `#14` 的执行方案：拆 `src/browser_login.py` | **已落地** —— 现为 `src/browser/` 包（阶段 A 文件内重构 + 阶段 B 拆包都完成；955 行 → 9 文件 / 1108 行） |
| [`credential-rotation-2026-09-19.md`](credential-rotation-2026-09-19.md) | 两次泄漏后的**凭据轮换清单** | ⚠ **待人工执行** —— 轮换要登录服务商后台，agent 做不了 |

> `refactor-plan-2026-09-19.md` §10.11 里写着「未完成：README 主体仍是 2,100+ 行……
> 拆它需要先定『哪一节属于哪一类』，属于**结构决策**而非机械搬运 —— 留待下一轮」。
> **那一轮就是 2026-09-20 的 B8**：README 的「协议要点」整段已迁到 [`protocol.md`](protocol.md)，
> 原地只留指针块。执行记录见 [`audit-2026-09-20.md`](audit-2026-09-20.md) §7「B8 执行记录」。
>
> **2026-10-06（第二轮）**：README 里剩下的**实测类**整段也已迁到 protocol.md ——
> 「429 限流的真实边界 / `workers` 的边界 / 注册配额是累计量限制 / 实测耗时」共约
> **750 行**，原地只留指针。README **1544 → 805 行**。同步修正了 `tools/probes/README.md`
> 里 7 处指向这些节的「`README「…」`」引用（B8 时遗留的同一类坑）。
>
> **2026-10-06（第三轮，按主题拆）**：`protocol.md` 自身再拆出两份自包含文档 ——
> [`mailbox.md`](mailbox.md)（邮箱源，477 行）与
> [`registration-limits.md`](registration-limits.md)（注册链路限流·配额·吞吐·耗时，764 行），
> `protocol.md` 1889 → 668 行。同轮还修掉了三处从 `docs/` 内部指向 `docs/protocol.md`
> 的**失效相对链接**（B8 搬运时遗留；`docs/` 内应为 `protocol.md`）。

## 相关

- **探针索引**：[`../tools/probes/README.md`](../tools/probes/README.md)
- **使用说明 / 架构 / 已知限制**：[`../README.md`](../README.md)

## 约定

1. **不带日期**的文件是现行文档；带日期的（`*-YYYY-MM-DD.md`）是冻结留档，不再更新。
2. **新增协议 / 实测结论请进 `protocol.md`**，不要写回 README —— README 只留
   「怎么用 + 架构 + 已知限制」。同一份知识写在两处必然漂移，且漂移方向不可预测。
3. 所有文档遵守 [`security-conventions.md`](security-conventions.md) 的占位符约定：
   不含真实凭据 / 出口 IP / 实例子域 / 本机绝对路径。
