"""CLI 入口。

用法：
  python run.py                          # 跑 1 个账号
  python run.py --count 6                # 跑 6 个（默认 4 路浏览器并发）
  python run.py --count 6 --workers 1    # 强制顺序执行（最保守）
  python run.py --count 6 --workers 6    # 6 路并发（实测安全，见下）
  python run.py --headful --count 6      # 有头（弹窗口），只在要肉眼看流程时用
  python run.py --out keys.json          # 显式导出到指定文件（不落台账快照）

关于 --out（台账落盘，2026-09-20 改）：
  **默认不填 = 写进台账目录** `ledger/`：
      ledger/runs/<日期>/results-<时间戳>.json   ← 合并后的**全量**快照（= 读源）
      ledger/latest.json                         ← **本批结果**（含失败 / 跳过）
  显式 `--out X` 才是老行为（只写 X、不落快照）。改布局的原因见
  `src/ledger.py` 的模块 docstring —— 老布局把"历史全量"和"本次结果"挤在
  仓库根的同一个 `results.json` 里，复盘时只能靠 `.bak-*` 的时间戳猜。

  ⚠ 台账的读源是**最新的那份全量快照**，**不是** `latest.json`。后者只含本批
    那几十条，拿它当读源 ⇒ 合并基准只剩上一批 ⇒ 台账停止累积、每次跑批覆盖
    上一次（本项目栽过两次，见 `src/ledger.py`）。

关于 --headless：
  **默认就是无头**（不弹窗口）。要弹窗口用 `--headful`。
  `--headless` 仍然接受，是为了让旧脚本/文档里的写法继续有效，不是必需的。

  ⚠ 别再宣称"无头更快" —— 2026-09-20 实测在 workers=4 量级上
  **无头与有头的吞吐没有可测差异**（两次无头 50 批次关键路径 194.2s / 196.5s，
  有头 100 批次 370.6s = 3.7s/账号；但 50 与 100 不可直接比，因为 50 有
  worker 空转的尾巴）。无头的真正好处是**不弹窗口**，不是速度。

关于 --workers：
  浏览器侧的并发数。注册阶段（纯 HTTP）由生产者池并发跑在前面，
  与浏览器阶段流水线重叠，所以 workers 不是"总并发"，而是"同时在跑的浏览器数"。

  🔴 默认值从 2 调到 4，依据是**实测**（`tools/probes/probe_login_only.py`，
  2026-09-15 晚，只测登录阶段以隔离掉注册配额这个混杂因素）：

      workers  账号数  总耗时   每账号   单账号中位   失败
        1        5     82.2s   16.4s     16.2s       0
        2        6     61.1s   10.2s     17.2s       0
        3        6     37.2s    6.2s     17.7s       0
        4        6     42.3s    7.1s     20.8s       0
        6        6     24.6s    4.1s     20.7s       0
        6       12     43.7s    3.6s     19.2s       0   ← 跨 2 轮，持续性验证

  结论：**浏览器侧并发到 6 都零失败、零 F001，单账号耗时几乎不退化**
  （中位 16→20s，轻微 CPU 争用，但吞吐是净赚）。
  早期"workers=4/6 失败"是**注册配额**，与浏览器并发无关 —— 别再把它们混为一谈。

  ⚠ 但 `workers=6` 只在**只测登录**时验证过；注册被 IP 封着，整链在 6 路下
    尚未复测。所以默认取 4（落在已验证的安全区间内），
    要更激进请显式 `--workers 6`。另外小批量时让 workers ≈ count
    （6 个账号用 4 路会有 2 路在第二轮空转）。
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import cli as _cli  # noqa: E402
from src import ledger as _ledger  # noqa: E402
from src import proxypool, redact  # noqa: E402
from src import report as _report  # noqa: E402
from src.pipeline import ERR_QUOTA, error_kind_of, run_batch  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="OpenXLab 注册 + API Key 提取")
    ap.add_argument("--count", type=int, default=1, help="注册账号数量")
    ap.add_argument(
        "--workers", type=int, default=4, help="浏览器并发数（实测 6 路零失败，默认 4）"
    )
    ap.add_argument("--key-name", default="default", help="API Key 名称")
    ap.add_argument("--mail-domain", default=None, help="临时邮箱域名（默认取 IR_WORKER_DOMAIN）")
    # `--headless` / `--headful` 的接线与 `tools/run_downstream.py` 共用
    # （见 src/cli.py 的模块 docstring：那一对开关的 `dest` 手写容易漏，
    #  漏了会让 `--headful` 静默无效）。
    _cli.add_headless_args(ap)
    ap.add_argument(
        "--out",
        default=None,
        help="结果输出文件。**不填（默认）= 写进台账目录**："
        "ledger/runs/<日期>/results-<时间戳>.json（合并后的全量快照，"
        "也是台账读源）+ ledger/latest.json（本批结果）。"
        "显式给路径则只写那一个文件、不落快照。",
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="只写本次结果、不合并历史（默认按 email 合并，防止一次小规模探测覆盖掉整个账号台账）",
    )
    ap.add_argument("--shot", default=None, help="保存过程截图的前缀")
    ap.add_argument("--quiet", action="store_true", help="只输出汇总")
    ap.add_argument(
        "--ignore-quota",
        action="store_true",
        help="跳过本地配额保护（仅当确信服务端配额已恢复时用）",
    )
    args = ap.parse_args()

    # 启动校验：缺凭据就立刻失败，别等跑了一半才发现全是 401。
    from common import config
    from src import quota

    missing = config.validate()
    if missing:
        print(f"✗ 缺少必需配置：{'、'.join(missing)}", file=sys.stderr)
        print(
            "  修法：cp .env.example .env 并填入真实值（.env 已在 .gitignore 中，不会进仓库）",
            file=sys.stderr,
        )
        return 1

    # 本地累计计数（跨运行）。这不是权威计量，但能在撞墙前把人拦住。
    slots = config.proxy_slots()
    # 🔴 必须带 `scope=""`（**单代理**那个出口），不能用无参的 `status()`：
    #    无参 = 统计**全部** scope。槽位模式下它就是"几个出口加起来"，与
    #    "我还能在单代理出口上注册几个"不是一回事。2026-10-04 实测：混合模式
    #    跑过之后无参得到 42/40（假性触顶），而 `scope=""` 是 **0/40** ——
    #    照无参判断会以退出码 2 结束、**一个请求都不发**，并让人白等 22 小时。
    #    顺带这也让下面那句"那是**老出口**的"变成真话（无参并不是老出口）。
    qs = quota.status(scope="")
    if slots:
        # 🔀 槽位模式下全局数字**没有参考价值** —— 它是池化之前那个出口的
        #    计数，而真正拦人的是每个出口 IP 各自的计数。所以这里必须
        #    按出口逐个打印，否则人会照着一个假的数字判断"还能跑多少"。
        print(
            f"🔀 槽位池：{len(slots)} 个槽位已配置"
            f"（{config.IR_PROXY_SLOTS_FILE or 'IR_PROXY_SLOTS'}）",
            flush=True,
        )
        total_left = 0
        try:
            for i, url in enumerate(slots, 1):
                ip = config.slot_scope(url)
                st = quota.status(scope=ip)
                left = max(0, config.REG_QUOTA_MAX - st.used)
                total_left += left
                mark = "已满" if st.exhausted else f"余 {left}"
                # 🔴 url 过一遍脱敏：槽位串可能是 `http://user:pass@host:port`，
                #    原样打印会把账密写进终端 / 重定向的日志。
                #    ⚠ 出口 IP 是**故意**打印的 —— 这张表的用途就是按出口看额度；
                #    但正因如此，**这段输出不要粘进任何仓库 / issue**（见
                #    docs/security-conventions.md「终端输出」一节）。
                print(
                    f"     slot{i} {redact.redact_url(url):<26} 出口 {ip:<16} "
                    f"{st.used:>2}/{config.REG_QUOTA_MAX}  {mark}",
                    flush=True,
                )
        except ValueError as ex:
            print(f"\n✗ 槽位出口 IP 未登记，拒绝开跑：\n  {ex}", file=sys.stderr)
            return 1
        print(
            f"   ── 合计可用额度 {total_left} 个"
            f"（全局计数 {qs.describe()} —— 那是**老出口**的，别拿它判断）",
            flush=True,
        )
        # 🔴 提示行由 `quota.shortfall_hint()` 统一生成 —— 那里要读
        #    `--ignore-quota`，否则开关打开时会打出"这一批会全部被跳过"
        #    这种**假话**（2026-09-20 实测：47/50 成功，提示却说全跳）。
        #    ⚠ 判据放在 `src/quota.py` 而不是这里的内联分支，理由见那个函数：
        #      内联没法单独测，而且**测试链不该 import CLI 模块**
        #      （`run.py` 会把整套 pipeline 拉进来，撞 test_dependency_surface）。
        hint = quota.shortfall_hint(total_left, args.count, args.ignore_quota)
        if hint:
            print(hint, flush=True)
    else:
        print(f"本地配额：{qs.describe()}  [state: {quota.state_path()}]", flush=True)
    if qs.exhausted and not args.ignore_quota and not slots:
        print(
            "  ⚠ 窗口内计数已达上限。这是**保守估计** —— 服务端恢复时间未知，"
            "本地窗口取的是偏保守值。\n"
            "    若确信服务端已恢复，可加 --ignore-quota 或调大 IR_REG_QUOTA_MAX。",
            flush=True,
        )

    t0 = time.time()
    try:
        results = run_batch(
            count=args.count,
            workers=args.workers,
            headless=args.headless,
            key_name=args.key_name,
            mail_domain=args.mail_domain,
            verbose=not args.quiet,
            screenshot_prefix=args.shot,
            ignore_quota=args.ignore_quota,
        )
    except quota.QuotaExceeded as ex:
        print(f"\n✗ {ex}", file=sys.stderr)
        print(
            "  这是**本地保护**（src/quota.py），不是服务端拒绝 —— 未发出任何请求。\n"
            "  选项：① 等窗口滑出（见上面的分钟数）；② 调大 IR_REG_QUOTA_MAX；\n"
            "        ③ 先确认服务端确实已恢复，再加 --ignore-quota。",
            file=sys.stderr,
        )
        return 2
    except proxypool.AllSlotsDead as ex:
        # 🔴 槽位端口预检失败。这里**刻意给一个干净的报错**而不是让它抛 traceback：
        #    这个失败模式的误读代价极高 —— 槽位进程没起来时，每条记录都会以
        #    "代理连接错误"收场，而"注册全失败"在本项目里最容易被读成
        #    "换 IP 也不行 / 还在封"。所以要把"是槽位没起来"这件事说在最前面。
        print(f"\n✗ {ex}", file=sys.stderr)
        return 4
    wall = time.time() - t0

    # 🔀 落盘位置（2026-09-20 改）
    #   * `--out` 不填（默认）→ 走**台账目录**：读 `ledger.ledger_path()`
    #     （= 最新那份全量快照），写 `ledger/runs/<日期>/results-<时间戳>.json`
    #     全量快照，再把**本批**结果刷进 `ledger/latest.json`。
    #   * `--out X` 显式给路径 → 老行为，只写 X、不落快照（"导出到别处"用）。
    # 改之前默认就是仓库根的 `results.json`，于是"历史全量"和"这次跑出来的"
    # 挤在同一个文件里，仓库根堆了一串手写 `.bak-batchXX-*` 备份，
    # 复盘时得靠时间戳猜哪个是哪个。
    out = Path(args.out) if args.out else None
    # ⚠ 这里的 `json.loads` 只可能在**我们自己的** `AccountRecord.to_json()`
    #    出问题时抛 —— 那是内部不变量被破坏。**绝不静默丢记录**（少数据但指标
    #    全"正常"是最坏的失败，见 `src/ledger.py` 的防静默缩水护栏），但要把
    #    肇事者说清楚：裸的 `JSONDecodeError` 看不出是哪条记录、哪个字段。
    try:
        new_records = [json.loads(r.to_json()) for r in results]
    except json.JSONDecodeError as ex:
        print(f"✗ 内部错误：AccountRecord.to_json() 产出了非法 JSON：{ex}", file=sys.stderr)
        return 5
    if args.overwrite:
        merged = new_records
        print(f"\n（--overwrite：只写本次 {len(merged)} 条，不合并历史）")
    else:
        # 🔴 读源用 `out or ledger_path()`：`--out` 不填时**必须**读台账读源。
        #    若这里退化成读"本次要写的那个新文件"，合并就等于没做 ——
        #    每次运行都从零开始，正是 `--count 1` 把 53 条覆盖成 1 条那条路。
        #    ⚠ 也不能读 `ledger/latest.json`（本批结果）：它只含上一批，
        #      同样会让台账停止累积 —— 这正是它**不是读源**的原因。
        existing = _ledger.load_existing(out or _ledger.ledger_path())
        merged, kept, added, upgraded = _ledger.merge_records(existing, new_records)
        if kept:
            # 格式与 `tools/run_downstream.py` 共用（src/cli.py）。
            # ⚠ 这里**有** `if kept:` 守着，那边没有 —— 那是刻意的差异，
            #   所以共用的是**文本**，不是"打印"这个动作。
            print(_cli.merge_summary_line(kept, added, upgraded, len(merged)))
    try:
        # 🔴 这里的判据是 `out is None`（= 用户没给 `--out`），
        #    **不是** `ledger.is_ledger_path(out)`。两者在
        #    "显式 `--out` 指向 ledger/ 内部" 这一种输入下**行为不同**：
        #    本入口会走纯导出（不落快照，见上面的 --out help），
        #    `tools/run_downstream.py` 会走快照分支。
        #    这是**有意的**，别"顺手统一"—— 统一会悄悄改掉本入口已承诺的语义。
        if out is None:
            # `new_records` 是**本批原始**结果（含失败 / 跳过）→ latest.json；
            # `merged` 是合并后的**全量** → 快照（也是新的读源）。
            snap, last = _ledger.save_snapshot(
                merged, new_records, existing=[] if args.overwrite else None
            )
            written = snap
            print(
                f"\n台账已落盘：\n"
                f"  快照（读源） {snap.relative_to(_ledger.ROOT)}\n"
                f"  本批结果     {last.relative_to(_ledger.ROOT)}"
                f"（{len(new_records)} 条）"
            )
        else:
            written = _ledger.save(out, merged, existing=[] if args.overwrite else None)
    except ValueError as ex:
        # 防静默缩水护栏（src/ledger）。少数据但指标全"正常"是最坏的失败，
        # 宁可报错退出也不要静默丢掉账号。
        print(f"✗ {ex}", file=sys.stderr)
        return 3

    # 收尾报告整块搬到 `src/report.py`（2026-09-20）。两个理由：
    #   ① 测试链不该 import `run.py` —— 它会把整套 pipeline 拉进 `sys.path`
    #      （见 `tests/test_dependency_surface.py`），而这块 stdout 是**逐字节
    #      契约**，必须有契约测试钉住，所以它得待在能被测试 import 的层；
    #   ② 这 ~180 行是纯展示逻辑，混在 CLI 入口里把真正的流程控制淹没了。
    # `error_kind_of` / `ERR_QUOTA` / `config` / `quota` 都是**注入**进去的，
    # 报告层因此只依赖 stdlib，不会把 `requests` 那条链带进测试。
    # 收尾那行的配额摘要**由这里渲染**（`report.py` 有意不知道"槽位"这个概念，
    # 只依赖注入对象，见它的 docstring）：槽位模式下 `quota.status()` 无参是
    # "各出口之和"，而它的 limit 是**单出口**上限 —— 直接印会假性说
    # "配额已用尽、N 分钟后可再注册"。
    quota_line = (
        quota.slots_summary(slots, scope_of=config.slot_scope)
        if slots
        else quota.status(scope="").describe()
    )
    _report.render_batch_report(
        results,
        written,
        wall,
        workers=args.workers,
        quota=quota,
        config=config,
        error_kind_of=error_kind_of,
        err_quota=ERR_QUOTA,
        quota_line=quota_line,
    )


if __name__ == "__main__":
    sys.exit(main())
