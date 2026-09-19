# 凭据轮换清单 — 2026-09-19

> ⚠️ **本文件不含任何凭据值**，也不含"前 6 位"这类信息 ——
> 高熵串的前缀足以在别处做关联。只写：键名、路径、处数、去哪换。
>
> 轮换需要登录服务商后台，**agent 做不了**。这份清单是给人执行的。

## 背景

2026-09-18 / 09-19 两次泄漏，其中第二次把**代理账密 + 8 个 IP** 写进了公开仓库
并推送。已于 2026-09-19 用 `git-filter-repo` 重写全部历史并 force push
（全历史逐 blob 扫描 0 命中）。

**但重写历史 ≠ 凭据安全**：本仓库有 **2 个 fork**，fork 共享对象库，
重写前的旧提交在它们里面仍然可达；GitHub 侧缓存与任何已存在的旧 clone 同理。

> **结论：凡是进过公开仓库的凭据，一律当作已泄漏，必须轮换。**

---

## 一、必须轮换

| # | 凭据 | 曾暴露的位置 | 处数 | 去哪换 | 优先级 |
|---|---|---|---|---|---|
| 1 | **代理节点账密**（`host:port:user:pass` 里的 user/pass） | 旧历史中 `tools/probe_proxy.py` 的用法示例行；当前 HEAD 已占位化 | 1 条串 | 代理服务商控制台 → 重置该节点/套餐的密码 | 🔴 高 |
| 2 | **代理出口 IP（4 个槽位出口 + 本机直连出口）** | 旧历史中 `README.md` / `src/proxypool.py` / `tools/probe_slots.py` / `src/config.py` | 5 个 IP | 无法"改" IP —— 视为**已烧毁**：换订阅 / 换节点 / 换机房，重测后重填 `IR_SLOT_EGRESS_IPS` | 🔴 高 |
| 3 | **代理服务商身份**（服务商名 + 套餐/订阅号） | 旧历史中 `tools/gen_mihomo_slots.py` 的 docstring 与注释 | 4 处 | 无需"换"，但要意识到它已公开 —— 评估是否需要换服务商 | 🟡 中 |

## 二、需确认（大概率未泄漏，但要自己核一遍）

| # | 凭据 | 为什么列出来 | 怎么确认 |
|---|---|---|---|
| 4 | **CF Worker Admin Token**（`IR_WORKER_ADMIN_TOKEN`） | 曾存在于磁盘上的 `.env.bak-20260918-110124`。该文件**未被 git 跟踪**（当时的 `.gitignore` 漏了它，但它没被 `git add` 过），所以**没进仓库**；文件已于 2026-09-19 删除 | `git log --all --oneline -S'<token 前 8 位>'` 应为 0；确认后可不轮换 |
| 5 | **CF API Token**（`cfat_` 前缀） | 曾在对话里明文粘贴过（会话通道，不是仓库通道） | 仓库侧已扫过：HEAD 与全历史均无 `cfat_` 命中。若在意会话通道，可在 CF 后台重新签发 |
| 6 | **已注册账号的邮箱密码 / API Key** | 运行产物 `results.json` / `keys_export.*` 从未被跟踪（`*.json` 规则挡住） | `git log --all --oneline -- results.json` 应为 0 |

## 三、不需要动的

| 对象 | 原因 |
|---|---|
| 目标站域名（`sso.openxlab.org.cn` 等） | 项目就是干这个的，公开无意义 |
| `client_id` | 每次请求都出现在 URL/请求体里，目标站本来就看得见 |
| 服务端 RSA 公钥 | 服务端静态公开值 |
| `IR_MIHOMO_EXE` 等本机路径 | 只在本机 `.env` 里，未入库；但它暴露了目录结构，所以**不要**写进任何文档 |

---

## 四、轮换后的收尾动作

1. **更新本机 `.env`** —— 新值只写这里（`.env` 被 `.gitignore` 的 `.env.*` 家族规则挡住）。
2. **重测出口 IP 并迁移台账**（换节点后出口 IP 会变）：
   ```bash
   python tools/probe_slots.py                    # 量出真实出口 IP
   # 把结果写进 .env 的 IR_SLOT_EGRESS_IPS
   python tools/migrate_quota_scope.py --apply    # 迁移台账（旧记录挂在旧 IP 名下）
   ```
3. **确认闸门在跑**：
   ```bash
   python tools/install_hooks.py        # 钩子不随仓库分发，新 clone 要重装
   python tools/selftest_check_leaks.py # 确认它真的会拦
   ```
4. **通知 fork 持有者**（如果认识）：让他们删 fork 重建。删不掉就只能接受"旧凭据已公开"这个事实 —— 这也是为什么第 1、2 项必须轮换。

---

## 五、怎么避免下次再列这张表

规范见 [`security-conventions.md`](security-conventions.md)。核心三条：

1. 凭据只进 `.env`，代码 `os.getenv(..., "")` 默认空；
2. 风控标识（含**出口 IP**、邀请码、订阅名）一律不进仓库，文档用占位符；
3. 提交前过闸门（`.gitignore` + `check_leaks.py` + CI），
   并且**定期跑 `selftest_check_leaks.py` 确认闸门没有静默失效**。
