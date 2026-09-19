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

用法：
    python tools/check_keys_alive.py                       # 全部 + 抽样 3 个真推理
    python tools/check_keys_alive.py --sample 5 --workers 8
    python tools/check_keys_alive.py --limit 10            # 只测前 10 把
"""

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / ".workbuddy-ai" / "exports" / "keys_export.csv"
DEFAULT_OUT = ROOT / ".workbuddy-ai" / "exports"


def probe_models(key: str) -> tuple:
    """`GET /v1/models`。返回 `(verdict, detail)`。"""
    import requests

    from src import config

    try:
        r = requests.get(f"{config.CHAT_API_BASE}/models",
                         headers={"Authorization": f"Bearer {key}"}, timeout=30)
        if r.status_code == 200:
            models = [m.get("id", "") for m in (r.json().get("data") or [])]
            return "alive", f"{len(models)} models"
        # 401/403 = 鉴权不过 → key 死了。其他码单独归类，别混成"死"。
        if r.status_code in (401, 403):
            return "dead", f"HTTP {r.status_code} {r.text[:120]}"
        return "error", f"HTTP {r.status_code} {r.text[:120]}"
    except Exception as ex:                                   # noqa: BLE001
        return "error", f"{type(ex).__name__}: {ex}"[:160]


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
    return True, (f"model={res.model} text={res.text[:20]!r}"
                  if res.text else
                  f"model={res.model} 正文为空 finish_reason={res.finish_reason!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="检查 API Key 存活性")
    ap.add_argument("--csv", default=str(DEFAULT_CSV), help="key 来源 CSV")
    ap.add_argument("--limit", type=int, default=0, help="只测前 N 把（0=全部）")
    ap.add_argument("--workers", type=int, default=8, help="并发数")
    ap.add_argument("--sample", type=int, default=3,
                    help="抽样几把做真实推理（0=跳过）")
    ap.add_argument("--model", default=None, help="推理用的模型（默认 config 第一个）")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    with Path(args.csv).open(encoding="utf-8-sig") as f:
        all_rows = list(csv.DictReader(f))
    # 前缀过滤：只认 `sk-`。**必须把丢掉的行数报出来** —— 否则一旦平台换了
    # key 前缀（或 CSV 列名变了），这里会静默少测，报告仍然"全绿"。
    # 本项目对"静默缩水"已经吃过一次亏（导出文件成了唯一副本那回）。
    rows = [r for r in all_rows if (r.get("api_key") or "").startswith("sk-")]
    skipped = len(all_rows) - len(rows)
    if skipped:
        print(f"⚠ 跳过 {skipped}/{len(all_rows)} 行：api_key 缺失或不以 'sk-' 开头"
              f"（若这是意外，说明 CSV 列名或 key 前缀变了，别当成'没有死 key'）")
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        print(f"✗ 从 {args.csv} 读不到 key（共 {len(all_rows)} 行）")
        return 1

    print(f"检查 {len(rows)} 把 key（并发 {args.workers}）→ {args.csv}")
    t0 = time.time()
    out = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(probe_models, r["api_key"]): r for r in rows}
        for f in as_completed(futs):
            r = futs[f]
            verdict, detail = f.result()
            out[r["api_key"]] = {"email": r["email"], "verdict": verdict,
                                 "detail": detail}
    dt = time.time() - t0

    alive = [k for k, v in out.items() if v["verdict"] == "alive"]
    dead = [k for k, v in out.items() if v["verdict"] == "dead"]
    err = [k for k, v in out.items() if v["verdict"] == "error"]

    print(f"\n{'=' * 70}")
    print(f"存活 {len(alive)}/{len(out)}   死亡 {len(dead)}   异常 {len(err)}"
          f"   （{dt:.1f}s）")
    if dead:
        print(f"\n死亡的 key（前 10）：")
        for k in dead[:10]:
            print(f"  {out[k]['email']:38s} {k[:16]}…  {out[k]['detail'][:70]}")
    if err:
        print(f"\n异常（非鉴权失败，别当成 key 死了）：")
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
            chat_bad += (not ok)
            print(f"  {'✓' if ok else '✗'} {out[k]['email']:38s} {detail}")

    print(f"\n结论：{len(alive)}/{len(out)} 把 key 通过鉴权"
          + (f"；抽样推理 {chat_ok}/{chat_ok + chat_bad} 成功" if args.sample else ""))
    print("=" * 70)

    op = Path(args.out)
    op.mkdir(parents=True, exist_ok=True)
    rep = op / "keys_alive.json"
    rep.write_text(json.dumps(
        {"checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
         "total": len(out), "alive": len(alive), "dead": len(dead),
         "error": len(err), "chat_sample_ok": chat_ok,
         "chat_sample_fail": chat_bad,
         "keys": out}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已落盘 {rep}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
