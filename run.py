"""CLI 入口。

用法：
  python run.py                          # 跑 1 个账号
  python run.py --count 6                # 跑 6 个（默认 2 路浏览器并发）
  python run.py --count 6 --workers 1    # 强制顺序执行（最保守）
  python run.py --count 6 --workers 3    # 3 路并发（需实测风控是否放行）
  python run.py --headless --count 6     # 无头 + 并发
  python run.py --out keys.json          # 结果落盘

关于 --workers：
  浏览器侧的并发数。注册阶段（纯 HTTP）由生产者池并发跑在前面，
  与浏览器阶段流水线重叠，所以 workers 不是"总并发"，而是"同时在跑的浏览器数"。
  ⚠ 这是**受风控约束**的参数：阿里云按 IP + 指纹 + 频率打分，
    同一出口 IP 上并发登录过多会开始 F001。默认 2，风控收紧时回退到 1。
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.pipeline import run_batch  # noqa: E402


def _fmt_ms(v):
    return f"{v / 1000:.1f}" if isinstance(v, (int, float)) else "-"


def main():
    ap = argparse.ArgumentParser(description="OpenXLab 注册 + API Key 提取")
    ap.add_argument("--count", type=int, default=1, help="注册账号数量")
    ap.add_argument("--workers", type=int, default=2,
                    help="浏览器并发数（受风控约束，默认 2）")
    ap.add_argument("--key-name", default="default", help="API Key 名称")
    ap.add_argument("--mail-domain", default=None, help="临时邮箱域名（默认 liziai.cloud）")
    ap.add_argument("--headless", action="store_true",
                    help="无头模式（实测可用，比 headful 快；不弹窗口）")
    ap.add_argument("--out", default="results.json", help="结果输出文件")
    ap.add_argument("--shot", default=None, help="保存过程截图的前缀")
    ap.add_argument("--quiet", action="store_true", help="只输出汇总")
    args = ap.parse_args()

    # 启动校验：缺凭据就立刻失败，别等跑了一半才发现全是 401。
    from src import config

    missing = config.validate()
    if missing:
        print(f"✗ 缺少必需配置：{'、'.join(missing)}", file=sys.stderr)
        print("  修法：cp .env.example .env 并填入真实值"
              "（.env 已在 .gitignore 中，不会进仓库）", file=sys.stderr)
        return 1

    t0 = time.time()
    results = run_batch(
        count=args.count,
        workers=args.workers,
        headless=args.headless,
        key_name=args.key_name,
        mail_domain=args.mail_domain,
        verbose=not args.quiet,
        screenshot_prefix=args.shot,
    )
    wall = time.time() - t0

    out = Path(args.out)
    out.write_text(json.dumps([json.loads(r.to_json()) for r in results],
                              ensure_ascii=False, indent=2), encoding="utf-8")

    ok = [r for r in results if r.status == "success"]
    bad = [r for r in results if r.status != "success"]

    print(f"\n{'=' * 72}")
    print(f"DONE: {len(ok)}/{len(results)} succeeded -> {out.resolve()}")

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
        for k, v in d.items():
            if k.endswith("_ms"):
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
            print(f"  {(r.email or '(未建邮箱)'):42s} {r.error[:90]}")

    if ok:
        print(f"\n调用方式（OpenAI 兼容）：")
        print(f"  base_url = {config.CHAT_API_BASE}")
        print(f"  model    = {config.CHAT_MODELS[0]}")
        print(f"  api_key  = {ok[0].api_key}")
        print(f"  ⚠ 不要用 chat.intern-ai.org.cn（那是网页版，要绑手机号）")
    print("=" * 72)


if __name__ == "__main__":
    sys.exit(main())
