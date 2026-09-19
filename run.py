"""CLI 入口。

用法：
  python run.py                          # 跑 1 个账号
  python run.py --count 6                # 跑 6 个（默认 4 路浏览器并发）
  python run.py --count 6 --workers 1    # 强制顺序执行（最保守）
  python run.py --count 6 --workers 6    # 6 路并发（实测安全，见下）
  python run.py --headless --count 6     # 无头 + 并发
  python run.py --out keys.json          # 结果落盘

关于 --workers：
  浏览器侧的并发数。注册阶段（纯 HTTP）由生产者池并发跑在前面，
  与浏览器阶段流水线重叠，所以 workers 不是"总并发"，而是"同时在跑的浏览器数"。

  🔴 默认值从 2 调到 4，依据是**实测**（`tools/probe_login_only.py`，
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

from src.ledger import load_existing as _load_existing  # noqa: E402
from src.ledger import merge_records as _merge_records  # noqa: E402
from src.ledger import save as _save_ledger  # noqa: E402
from src.pipeline import run_batch  # noqa: E402


def _fmt_ms(v):
    return f"{v / 1000:.1f}" if isinstance(v, (int, float)) else "-"


def main():
    ap = argparse.ArgumentParser(description="OpenXLab 注册 + API Key 提取")
    ap.add_argument("--count", type=int, default=1, help="注册账号数量")
    ap.add_argument("--workers", type=int, default=4,
                    help="浏览器并发数（实测 6 路零失败，默认 4）")
    ap.add_argument("--key-name", default="default", help="API Key 名称")
    ap.add_argument("--mail-domain", default=None, help="临时邮箱域名（默认 <your-mail-domain>）")
    ap.add_argument("--headless", action="store_true",
                    help="无头模式（实测可用，比 headful 快；不弹窗口）")
    ap.add_argument("--out", default="results.json", help="结果输出文件")
    ap.add_argument("--overwrite", action="store_true",
                    help="只写本次结果、不合并历史（默认按 email 合并，"
                         "防止一次小规模探测覆盖掉整个账号台账）")
    ap.add_argument("--shot", default=None, help="保存过程截图的前缀")
    ap.add_argument("--quiet", action="store_true", help="只输出汇总")
    ap.add_argument("--ignore-quota", action="store_true",
                    help="跳过本地配额保护（仅当确信服务端配额已恢复时用）")
    args = ap.parse_args()

    # 启动校验：缺凭据就立刻失败，别等跑了一半才发现全是 401。
    from src import config
    from src import quota

    missing = config.validate()
    if missing:
        print(f"✗ 缺少必需配置：{'、'.join(missing)}", file=sys.stderr)
        print("  修法：cp .env.example .env 并填入真实值"
              "（.env 已在 .gitignore 中，不会进仓库）", file=sys.stderr)
        return 1

    # 本地累计计数（跨运行）。这不是权威计量，但能在撞墙前把人拦住。
    slots = config.proxy_slots()
    qs = quota.status()
    if slots:
        # 🔀 槽位模式下这个全局数字**属于老出口**（换槽位之前那个 IP），
        #    对新出口没有参考价值 —— 真正生效的是每个槽位各自的计数。
        print(f"🔀 槽位池：{len(slots)} 个槽位已配置"
              f"（{config.IR_PROXY_SLOTS_FILE or 'IR_PROXY_SLOTS'}）\n"
              f"   全局本地配额 {qs.describe()} —— ⚠ 这是**老出口**的计数，"
              f"槽位模式下按出口分别计，不拿它拦人。", flush=True)
    else:
        print(f"本地配额：{qs.describe()}  "
              f"[state: {quota.state_path()}]", flush=True)
    if qs.exhausted and not args.ignore_quota and not slots:
        print("  ⚠ 窗口内计数已达上限。这是**保守估计** —— 服务端恢复时间未知，"
              "本地窗口取的是偏保守值。\n"
              "    若确信服务端已恢复，可加 --ignore-quota 或调大 IR_REG_QUOTA_MAX。",
              flush=True)

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
        print("  这是**本地保护**（src/quota.py），不是服务端拒绝 —— 未发出任何请求。\n"
              "  选项：① 等窗口滑出（见上面的分钟数）；② 调大 IR_REG_QUOTA_MAX；\n"
              "        ③ 先确认服务端确实已恢复，再加 --ignore-quota。",
              file=sys.stderr)
        return 2
    wall = time.time() - t0

    out = Path(args.out)
    new_records = [json.loads(r.to_json()) for r in results]
    if args.overwrite:
        merged = new_records
        print(f"\n（--overwrite：只写本次 {len(merged)} 条，不合并历史）")
    else:
        existing = _load_existing(out)
        merged, kept, added, upgraded = _merge_records(existing, new_records)
        if kept:
            print(f"\n结果合并：原有 {kept} 条 + 本次新增 {added} 条"
                  + (f"（{upgraded} 条已更新：升级或补全字段）" if upgraded else "")
                  + f" = {len(merged)} 条")
    try:
        _save_ledger(out, merged, existing=[] if args.overwrite else None)
    except ValueError as ex:
        # 防静默缩水护栏（src/ledger.save）。少数据但指标全"正常"是最坏的失败，
        # 宁可报错退出也不要静默丢掉账号。
        print(f"✗ {ex}", file=sys.stderr)
        return 3

    ok = [r for r in results if r.status == "success"]
    skipped = [r for r in results if r.status == "skipped"]
    bad = [r for r in results if r.status not in ("success", "skipped")]

    print(f"\n{'=' * 72}")
    print(f"DONE: {len(ok)}/{len(results)} succeeded -> {out.resolve()}")
    if skipped:
        print(f"      {len(skipped)} 个被配额保护跳过（未发请求，非失败）")

    # 耗时明细：注册 / 登录 / 建Key 三段，定位瓶颈用
    print(f"\n耗时明细（秒）：")
    print(f"  {'email':40s} {'注册':>7s} {'登录':>7s} {'建Key':>7s} {'合计':>7s}")
    for r in results:
        tm = r.timings or {}
        print(f"  {(r.email or '(未建邮箱)'):40s} "
              f"{_fmt_ms(tm.get('register')):>7s} "
              f"{_fmt_ms(tm.get('login')):>7s} "
              f"{_fmt_ms(tm.get('key')):>7s} "
              f"{_fmt_ms(tm.get('total') or tm.get('batch_total')):>7s}")

    # 登录内部阶段。
    # 🔴 看**最慢**的那个，不是第一个 —— 批量吞吐由关键路径（最慢账号）决定，
    #    离群值才是要找的东西。只打印第一个账号会把离群值藏起来。
    #    （实测教训：E4b 里 workerA 被一个 32.5s 的登录卡住，
    #      逼 workerB 接手后面 4 个账号，总时长被方差而非均值主导。）
    slow = None
    for r in ok:
        detail = (r.timings or {}).get("login_detail")
        if not detail:
            continue
        if slow is None or r.timings.get("login", 0) > slow.timings.get("login", 0):
            slow = r
    if slow is not None:
        detail = slow.timings["login_detail"]
        print(f"\n登录内部阶段（最慢账号 {slow.email}，"
              f"登录 {_fmt_ms(slow.timings.get('login'))}s）：")
        prev = 0
        for k, v in detail.items():
            print(f"  {k:16s} +{(v - prev) / 1000:6.2f}s   (累计 {v / 1000:5.2f}s)")
            prev = v
        cs = (slow.stages or {}).get("captcha_path")
        if cs:
            print(f"  验证码通路       Path {cs}"
                  f"{'（TRACELESS 自过，零点击）' if cs == 'A' else ''}")

    # 注册内部阶段（取最慢账号，同样理由）
    slow_reg = None
    for r in results:
        sub = (r.timings or {}).get("register_detail")
        if not sub:
            continue
        if slow_reg is None or (r.timings or {}).get("register", 0) > \
                (slow_reg.timings or {}).get("register", 0):
            slow_reg = r
    if slow_reg is not None:
        print(f"\n注册内部阶段（最慢账号 {slow_reg.email or '(未建邮箱)'}，"
              f"注册 {_fmt_ms(slow_reg.timings.get('register'))}s）：")
        d = slow_reg.timings["register_detail"]
        # 🔴 `register_detail` 里绝大部分键是**毫秒**（下面统一 /1000），
        #    但计数类字段不是 —— 混进去会打印成 "0.05s"，看着像个耗时。
        COUNT_KEYS = {"mail_polls", "mail_5xx"}
        for k, v in d.items():
            if k.endswith("_ms") or k in COUNT_KEYS:
                continue
            label = k
            if k == "mail_wait":
                # 拆开看：邮件真正到达 vs 我们的轮询开销。两者修法完全不同。
                ad, po = d.get("arrival_delay_ms"), d.get("poll_overhead_ms")
                if ad is not None and po is not None:
                    label = (f"mail_wait        （到达 {ad / 1000:.2f}s + "
                             f"轮询 {po / 1000:.2f}s）")
                    print(f"  {label}")
                    continue
            print(f"  {label:16s} {v / 1000:6.2f}s")
        # 收信打了几次收信接口 —— 背后是 D1。
        # 这是"注册一个账号花掉多少 D1 读取"的唯一凭据。
        # 2026-09-19 起走 `/api/inbox?email=`（每次 0~1 行），
        # 改造前走 `/admin/all`（每次 51 行）。
        polls = d.get("mail_polls")
        if polls:
            e5 = d.get("mail_5xx", 0)
            extra = f"，其中 5xx 重试 {e5} 次" if e5 else ""
            print(f"  {'收信轮询':14s} {polls:6d} 次 /api/inbox{extra}")

    # 登录耗时离散度 —— 方差比均值更能解释批量总时长
    logins = sorted(r.timings.get("login", 0) / 1000
                    for r in ok if r.timings.get("login"))
    if len(logins) >= 2:
        print(f"\n登录耗时分布（{len(logins)} 个账号）：")
        print(f"  最快 {logins[0]:.1f}s · 中位 {logins[len(logins) // 2]:.1f}s "
              f"· 最慢 {logins[-1]:.1f}s · 极差 {logins[-1] - logins[0]:.1f}s")
        if logins[-1] > logins[0] * 1.5:
            print(f"  ⚠ 极差超过最快值的 1.5 倍 —— 总时长多半由这个离群值决定，"
                  f"不是均值")

    if results:
        tm0 = results[0].timings or {}
        kd, bt = tm0.get("keys_done"), tm0.get("batch_total")
        print(f"\n吞吐（{len(results)} 个账号，workers={args.workers}）：")
        if kd and bt:
            print(f"  关键路径（全部 key 建出）: {kd / 1000:6.1f}s "
                  f"= {kd / 1000 / len(results):5.1f}s / 账号")
            print(f"  含末尾统一校验          : {bt / 1000:6.1f}s "
                  f"= {bt / 1000 / len(results):5.1f}s / 账号")
            rounds = -(-len(results) // args.workers)     # ceil
            if rounds * args.workers != len(results):
                print(f"  ⚠ {len(results)} 个账号 / {args.workers} 路 = {rounds} 轮，"
                      f"最后一轮有 worker 空转。"
                      f"凑成 {rounds * args.workers} 个能摊得更薄。")
        else:
            print(f"  总耗时 {wall:6.1f}s = {wall / len(results):5.1f}s / 账号")

    for r in ok:
        print(f"  {r.email:42s} {r.api_key}")
    if bad:
        print(f"\n失败 {len(bad)} 个：")
        for r in bad:
            print(f"  {(r.email or '(未建邮箱)'):42s} {(r.error or '')[:90]}")

        # 🔴 注册配额触顶是批量失败最常见的原因，且**重试无用**。
        #    实测（2026-09-15）：同一时段累计注册约 40 个账号后开始出现
        #    `B0000 请求频繁`，之后**连单账号都注册不了**，等了几分钟仍未恢复。
        #    ⚠ 它是**累计量**级别的限制 —— 调大 REG_MIN_INTERVAL（瞬时速率闸门）
        #      完全无效，不要往那个方向排查。
        #    现在有了本地计数（src/quota.py）：开跑前拦 + 运行中 fail-fast。
        #    这里还有残留的，说明本地计数上限（REG_QUOTA_MAX）比服务端真实阈值高。
        quota_hits = [r for r in bad if "B0000" in (r.error or "")]
        if quota_hits:
            print(f"\n  ⚠ 其中 {len(quota_hits)} 个是注册配额触顶（B0000 请求频繁）")
            print(f"    这是**累计量**限制，不是瞬时速率 —— 调 REG_MIN_INTERVAL 无效。")
            print(f"    实测：同一时段累计约 40 个账号后触发，需等待窗口恢复后再跑。")
            print(f"    → 建议把 IR_REG_QUOTA_MAX 下调到本次触顶点，让本地保护更早拦住。")

    if ok:
        print(f"\n调用方式（OpenAI 兼容）：")
        print(f"  base_url = {config.CHAT_API_BASE}")
        print(f"  model    = {config.CHAT_MODELS[0]}")
        print(f"  api_key  = {ok[0].api_key}")
        print(f"  ⚠ 不要用 chat.intern-ai.org.cn（那是网页版，要绑手机号）")

    # 收尾再报一次配额，让"本次消耗了几个"一眼可见。
    qs2 = quota.status()
    print(f"\n本地配额：{qs2.describe()}（本次成功 {len(ok)} 个，"
          f"跳过 {len(skipped)} 个）")
    print("=" * 72)


if __name__ == "__main__":
    sys.exit(main())
