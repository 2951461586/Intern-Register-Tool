"""检查导出的 API Key 是否**还活着**（走推理网关，与注册封禁无关）。

为什么值得单独做
----------------
key 建出来时都验过（`stages["verify"] == "ok(10 models)"`），但那是**当时**。
之后平台可能：回收额度、封 key、重置账号 —— 导出的列表里有多少还能用，
不测就不知道。而导出列表是拿来**用**的，里面混着死 key 会浪费排查时间。

两级验证（照本项目「验证必须用真实业务调用收尾」的规矩）
--------------------------------------------------------
1. **全部 key**：`GET /v1/models` —— 这是一个**真实鉴权**调用，
   401/403 就说明 key 死了；但"能列模型"仍不等于"能推理"。
2. **抽样 key**：真发一次 `chat/completions` —— 只有这一步能证明**真能用**。
   （本项目吃过亏：接口返回 200 + 一个 sk- 字符串，并不等于这个 key 能用。）

⚠ 与注册无关：打的是 `discovery-api.intern-ai.org.cn`，不是注册接口，
  所以**不会**影响注册封禁的状态，可以放心跑。

🔴 默认输入是**某次导出的 CSV 快照**，不是台账 —— 快照会过期。
  工具因此带一道"文件级防静默缩水"护栏：拿台账比对，快照覆盖不全就告警。
  没有这道护栏时，快照停在几天前会让你看到"53/53 全绿"这种**没测到却像全绿**的结论
  （2026-09-20 实测踩到）。

🔴 护栏**不止告警**，还得落在两处（2026-09-21 补）：
  1. `keys_alive.json` 的 `coverage` 块 —— 否则事后单独看这份 artifact
     （或由它派生的报告）就是"53/53 全绿"，**artifact 里连分母这个概念都没有**；
  2. **退出码** —— 覆盖不足返回 `ledger.EXIT_COVERAGE_GAP`（3，与 `run.py`
     的防静默缩水护栏同码）。只打印不改退出码的话，脚本化调用读到的是"成功"。
  确实只想核验一个子集时，加 `--allow-partial` 显式放行。

用法：
    python tools/ops/check_keys_alive.py                       # 全部 + 抽样 3 个真推理
    python tools/ops/check_keys_alive.py --sample 5 --workers 8
    python tools/ops/check_keys_alive.py --limit 10            # 只测前 10 把
    python tools/ops/check_keys_alive.py --csv <自己导出的清单> # 核验指定批次
    python tools/ops/check_keys_alive.py --allow-partial       # 明知只覆盖子集，别报退出码

🔴 核验存活率必须加 `--interval`（>0 会自动串行）
------------------------------------------------
429 是**按 IP 累积的短窗口限流**，唯一能压住它的是**降速率**，不是降并发。
2026-09-22 实测（100 批次复核）：并发 8 时 47 把里 **37 把**吃 429，汇总行读起来
像"只有 10 把能用"；改串行 + 间隔 5s 后 **97/97 全活、死亡 0**。
⇒ 429 现在单独成一档（`rate_limited`）并**退避重试**（`--backoff` / `--max-retry`）；
   它**不进** `dead` —— `dead` 只认 401/403 这种确定性拒绝。

退出码：0=正常（且覆盖完整）；1=从 CSV 读不到 key；3=覆盖不足（护栏触发）。
"""

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from _path import ROOT  # noqa: F401  （副作用：把 tools/ 与仓库根加进 sys.path）

from src import apikey as _ak  # noqa: E402  （判定策略：verdict_of_status / rate_limit_backoff）
from src import ledger  # noqa: E402  （必须在 _path 之后：它才把仓库根加进 sys.path）

# key 前缀只在这里定义一次，行级过滤与台账覆盖统计共用 —— 两边口径不一致
# 会互相掩盖（一边当 key、另一边当噪音）。
KEY_PREFIX = ledger.KEY_PREFIX

DEFAULT_CSV = ROOT / ".workbuddy-ai" / "exports" / "keys_export.csv"
DEFAULT_LEDGER = ledger.ledger_path()
DEFAULT_OUT = ROOT / ".workbuddy-ai" / "exports"

# 429 退避的默认值。放模块层，好让 --help 与测试读到同一个数。
DEFAULT_BACKOFF = 20.0
DEFAULT_MAX_RETRY = 3


def probe_models(
    key: str, *, backoff: float = DEFAULT_BACKOFF, max_retry: int = DEFAULT_MAX_RETRY
) -> tuple:
    """`GET /v1/models`。返回 `(verdict, detail)`。

    🔴 **429 单独成一档，并且退避重试** —— 它不是 key 的问题，是"你打太快了"。
    旧版把 429 归进 `error`，于是**复核自己造成的限流**读起来像"这把 key 有问题"：
    2026-09-22 实测并发 8 时 47 把里 37 把是 429，差点得出"只有 10 把能用"；
    换串行 + 间隔 5s 后 **97/97 全活、死亡 0**。

    判定走 `apikey.verdict_of_status()`（纯函数，放 `src/` 才能单独测）；
    退避时长走 `apikey.rate_limit_backoff()`（线性加长 + 封顶）。
    """
    import requests

    from common import config

    last = ""
    for attempt in range(max_retry + 1):
        try:
            r = requests.get(
                f"{config.CHAT_API_BASE}/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=30,
            )
        except Exception as ex:  # noqa: BLE001
            return _ak.VERDICT_ERROR, f"{type(ex).__name__}: {ex}"[:160]

        verdict = _ak.verdict_of_status(r.status_code)
        if verdict == _ak.VERDICT_ALIVE:
            models = [m.get("id", "") for m in (r.json().get("data") or [])]
            return verdict, f"{len(models)} models"
        if verdict == _ak.VERDICT_DEAD:
            # 只有 401/403 会走到这里 —— 其余码一律不算"key 死了"。
            return verdict, f"HTTP {r.status_code} {r.text[:120]}"
        if verdict == _ak.VERDICT_RATE_LIMITED:
            last = f"HTTP 429 {r.text[:120]}"
            if attempt < max_retry:
                delay = _ak.rate_limit_backoff(attempt, base=backoff)
                print(f"    · 429 → 退避 {delay:.0f}s 后重试（{attempt + 1}/{max_retry}）")
                time.sleep(delay)
                continue
            return verdict, f"{last}（退避重试 {attempt + 1} 次仍是 429）"
        return verdict, f"HTTP {r.status_code} {r.text[:120]}"
    return _ak.VERDICT_ERROR, last  # pragma: no cover


def probe_chat(key: str, model: str = None) -> tuple:
    """真发一次推理 —— 唯一能证明"真能用"的判据。

    🔴 `max_tokens` 不能给太小：本项目实测（2026-09-16）默认模型
    `deepseek-v4-flash-0731` 是**带 reasoning 的模型**，`max_tokens=32` 时
    `reasoning_tokens=34` 就把预算吃光了 → 返回 200 但 `content` 为空、
    `finish_reason="length"`。旧版这里用 32，会把一把**好 key** 报成
    "推理没输出"。现在给 128，并显式区分"被截断"和"真失败"。
    """
    from src import apikey as _ak

    res = _ak.chat(key, "只回复两个字：成功", model=model, max_tokens=128)
    if not res.ok:
        return False, res.error[:160]
    if res.truncated:
        # 网关通了、模型也在推理，只是没留够 token 写正文 —— 不是 key 的问题
        return True, f"model={res.model} 正文被 max_tokens 截断（reasoning 占满）"
    return True, (
        f"model={res.model} text={res.text[:20]!r}"
        if res.text
        else f"model={res.model} 正文为空 finish_reason={res.finish_reason!r}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="检查 API Key 存活性")
    ap.add_argument("--csv", default=str(DEFAULT_CSV), help="key 来源 CSV")
    ap.add_argument(
        "--ledger",
        default=str(DEFAULT_LEDGER),
        help="权威台账（只用来检查快照是否过期，不参与测试）",
    )
    ap.add_argument("--limit", type=int, default=0, help="只测前 N 把（0=全部）")
    ap.add_argument("--workers", type=int, default=8, help="并发数")
    ap.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="每把 key 之间的间隔秒数；>0 会**强制串行**（429 是降速率问题，不是降并发问题）",
    )
    ap.add_argument(
        "--backoff",
        type=float,
        default=DEFAULT_BACKOFF,
        help=f"命中 429 后的退避基数秒（线性加长，封顶 {_ak.RATE_LIMIT_BACKOFF_CAP:.0f}s）",
    )
    ap.add_argument(
        "--max-retry",
        type=int,
        default=DEFAULT_MAX_RETRY,
        help="命中 429 时最多重试几次（0=不重试，直接报 rate_limited）",
    )
    ap.add_argument("--sample", type=int, default=3, help="抽样几把做真实推理（0=跳过）")
    ap.add_argument("--model", default=None, help="推理用的模型（默认 config 第一个）")
    ap.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="输出**目录**（工具会在其中写 keys_alive.json，不是文件路径）",
    )
    ap.add_argument(
        "--allow-partial",
        action="store_true",
        help="允许快照只覆盖台账的一部分（默认覆盖不足会返回退出码 3）",
    )
    args = ap.parse_args()

    with Path(args.csv).open(encoding="utf-8-sig") as f:
        all_rows = list(csv.DictReader(f))
    # 前缀过滤：只认 `sk-`。**必须把丢掉的行数报出来** —— 否则一旦平台换了
    # key 前缀（或 CSV 列名变了），这里会静默少测，报告仍然"全绿"。
    # 本项目对"静默缩水"已经吃过一次亏（导出文件成了唯一副本那回）。
    rows = [r for r in all_rows if (r.get("api_key") or "").startswith(KEY_PREFIX)]
    skipped = len(all_rows) - len(rows)
    if skipped:
        print(
            f"⚠ 跳过 {skipped}/{len(all_rows)} 行：api_key 缺失或不以 '{KEY_PREFIX}' 开头"
            f"（若这是意外，说明 CSV 列名或 key 前缀变了，别当成'没有死 key'）"
        )

    # ── 文件级防静默缩水：整个 CSV 可能已经过期 ────────────────────
    # 上面那段管的是**行级**缩水（分母变了）；这段管**文件级**缩水 ——
    # 快照整体停在几天前时，连分母都是错的，只报"存活 N/N"看不出问题。
    # 实测（2026-09-20）：快照 53 把 / 台账 407 把，跑出"53/53 全绿"，
    # 看着没问题，其实完全没覆盖当时那一批。
    ledger_n, csv_n, missing = ledger.key_coverage(
        ledger.load_existing(args.ledger), {r["api_key"] for r in rows}
    )
    # 判据放在 `src/ledger.py`（纯函数），这里只接线 —— 理由同 `quota.shortfall_hint`：
    # 内联分支没法单独测，而测试链不该 import 本脚本（它要发真实网络请求）。
    gap_rc = ledger.coverage_exit_code(missing, allow_partial=args.allow_partial)
    coverage = ledger.coverage_block(
        ledger_n, csv_n, missing, ledger=args.ledger, snapshot=args.csv
    )
    if missing:
        print(
            f"⚠ 导出快照**落后于台账**：台账 {ledger_n} 把带 key / 快照 {csv_n} 把，"
            f"本次结论**不覆盖**台账里多出的 {len(missing)} 把。"
        )
        print(f"    快照：{args.csv}")
        print(f"    台账：{args.ledger}")
        print(
            "    ⇒ 这不是'没有死 key'，是**没测到**。"
            "要核验全量请先按当前台账重新导出，或改用别的取样口径。"
        )
        if gap_rc:
            print(f"    ⇒ 退出码 {gap_rc}（护栏触发）。确实只想核验一个子集请加 --allow-partial。")

    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print(f"✗ 从 {args.csv} 读不到 key（共 {len(all_rows)} 行）")
        return 1

    # ── 速率护栏 ────────────────────────────────────────────────
    # 429 是**按 IP 累积的短窗口限流** ⇒ 唯一能压住它的是**降速率**，不是降并发。
    # 2026-09-22 实测：并发 8 时 47 把里 37 把吃 429，读起来像"只有 10 把能用"；
    # 串行 + 间隔 5s ⇒ 97/97 全活、0 死亡。下面两条提示就是那次教训。
    if args.interval > 0 and args.workers != 1:
        print(f"⚠ --interval {args.interval}s 是**降速率**手段，与并发 {args.workers} 冲突。")
        print("   ⇒ 已强制串行（要保留并发请传 --interval 0）。")
        args.workers = 1
    if args.interval <= 0 and args.workers > 1:
        print(f"⚠ 并发 {args.workers} 且无间隔 —— 这是**已知会打出假 429** 的配置：")
        print("   429 会被退避重试吸收，但结果里仍可能出现 `rate_limited` 档，")
        print("   那些 key 的存活状态是**未知**，不是失败。核验存活率请用 --interval 5。")

    mode = f"串行 + 间隔 {args.interval}s" if args.interval > 0 else f"并发 {args.workers}"
    print(f"检查 {len(rows)} 把 key（{mode}）→ {args.csv}")
    t0 = time.time()
    out = {}

    def _record(r: dict, verdict: str, detail: str) -> None:
        out[r["api_key"]] = {"email": r["email"], "verdict": verdict, "detail": detail}

    if args.interval > 0:
        # 串行：每把之间隔 interval 秒。**最后一把之后不睡**，别白等。
        for i, r in enumerate(rows):
            verdict, detail = probe_models(
                r["api_key"], backoff=args.backoff, max_retry=args.max_retry
            )
            _record(r, verdict, detail)
            if i < len(rows) - 1:
                time.sleep(args.interval)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {
                ex.submit(
                    probe_models, r["api_key"], backoff=args.backoff, max_retry=args.max_retry
                ): r
                for r in rows
            }
            for f in as_completed(futs):
                r = futs[f]
                verdict, detail = f.result()
                _record(r, verdict, detail)
    dt = time.time() - t0

    alive = [k for k, v in out.items() if v["verdict"] == _ak.VERDICT_ALIVE]
    dead = [k for k, v in out.items() if v["verdict"] == _ak.VERDICT_DEAD]
    rl = [k for k, v in out.items() if v["verdict"] == _ak.VERDICT_RATE_LIMITED]
    err = [k for k, v in out.items() if v["verdict"] == _ak.VERDICT_ERROR]

    rate = (len(out) * 60.0 / dt) if dt else 0.0
    print(f"\n{'=' * 70}")
    print(
        f"存活 {len(alive)}/{len(out)}   死亡 {len(dead)}   "
        f"限流 {len(rl)}   异常 {len(err)}   （{dt:.1f}s，{rate:.1f} 把/分）"
    )
    if dead:
        print("\n死亡的 key（前 10）—— 只有 401/403 会进这里：")
        for k in dead[:10]:
            print(f"  {out[k]['email']:38s} {k[:16]}…  {out[k]['detail'][:70]}")
    if rl:
        print("\n限流的 key（**不是 key 的问题**，是你打太快了）：")
        print("  ⇒ 这些 key 的存活状态**未知**，别当成失败。重跑请加 --interval 5。")
        for k in rl[:10]:
            print(f"  {out[k]['email']:38s} {out[k]['detail'][:80]}")
    if err:
        print("\n异常（非鉴权失败，别当成 key 死了）：")
        for k in err[:10]:
            print(f"  {out[k]['email']:38s} {out[k]['detail'][:80]}")

    # ── 抽样真实推理 ──────────────────────────────────────────
    chat_ok = chat_bad = 0
    if args.sample and alive:
        n = min(args.sample, len(alive))
        print(f"\n抽样 {n} 把做**真实推理**（只有这一步能证明真能用）：")
        for k in alive[:n]:
            ok, detail = probe_chat(k, args.model)
            out[k]["chat_ok"] = ok
            out[k]["chat_detail"] = detail
            chat_ok += ok
            chat_bad += not ok
            print(f"  {'✓' if ok else '✗'} {out[k]['email']:38s} {detail}")

    print(
        f"\n结论：{len(alive)}/{len(out)} 把 key 通过鉴权"
        + (f"；**{len(rl)} 把限流、存活状态未知**" if rl else "")
        + (f"；抽样推理 {chat_ok}/{chat_ok + chat_bad} 成功" if args.sample else "")
    )
    print("=" * 70)

    op = Path(args.out)
    op.mkdir(parents=True, exist_ok=True)
    rep = op / "keys_alive.json"
    rep.write_text(
        json.dumps(
            {
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "total": len(out),
                "alive": len(alive),
                "dead": len(dead),
                # 🔴 429 单独进 artifact：它**不是** key 的结论，而是"这次测法太快了"。
                #    混进 dead/error 都会让事后单独打开这份 JSON 的人读错。
                "rate_limited": len(rl),
                "error": len(err),
                "interval_s": args.interval,
                "backoff_s": args.backoff,
                "chat_sample_ok": chat_ok,
                "chat_sample_fail": chat_bad,
                # 🔴 `coverage` 必须进 artifact：光打印的话，几天后单独打开这份 JSON
                #    就是"total=53, alive=53"，看不出它只覆盖了台账的 53/605。
                "coverage": coverage,
                "keys": out,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"报告已落盘 {rep}")
    return gap_rc


if __name__ == "__main__":
    sys.exit(main())
