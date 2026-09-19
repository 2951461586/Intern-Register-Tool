"""注册配额的本地累计计数与保护。

背景
----
实测（2026-09-15）：`register/byEmail` 除瞬时速率限制（见
`pipeline.REG_MIN_INTERVAL`）外，还有一层**累计配额**：

    同一时段累计注册约 40 个账号后开始返回 `B0000 请求频繁`，
    之后连**单账号**都注册不了。

🔴 **恢复窗口 > 8.6h**（这是两次观测逼出来的修正）：
   最后成功注册 `14:05:43` → 同一晚 `22:39`（8.6h 后）单账号探测**仍然 B0000**。
   最初的估计是"等数分钟仍未恢复"，据此取了 6h 窗口 —— **那不是保守，是太短**，
   会让保护在服务端仍封着时放行。现在取 24h 作为保守上界。

🔴 封禁是 **IP 维度**，不是邮箱域名维度（实测，见 `tools/probe_quota_scope.py`）：
   同 IP、只换发信域名（`<your-mail-domain>` → `<alt-domain>`，Worker 支持 7 个域名）
   → **两个域名都返回 B0000**。所以换域名没用，只能等窗口或换出口 IP。

三个判据确认它是累计量而非速率（详见 README「注册配额是累计量限制」）：
  1. 失败**全部**落在 register 阶段（不是登录）
  2. 改 `workers` 无效 —— 它不控制注册并发度
  3. 调 `REG_MIN_INTERVAL` 无效 —— 那是瞬时闸门

所以需要一份**跨进程、跨运行**的本地计数，在接近上限时主动停下，
而不是撞上去之后才发现。

存储
----
`.workbuddy-ai/state/register_quota.jsonl`（已被 .gitignore 排除）
可用 `IR_QUOTA_STATE` 覆盖路径 —— 自检脚本靠它做隔离，多份计数并存也靠它。

**JSONL 追加写**，每行 `{"ts": <unix秒>, "email": "...", "scope": "..."}`。
选追加写而不是 read-modify-write 是为了**并发安全**：多个 producer 线程
各自 append 不会互相覆盖。只记成功 —— 失败的不占配额。

`scope` = **出口作用域**（槽位池模式下是 `"slot3"`，否则空串）。
🔴 服务端配额是按**出口 IP** 记的，所以计数也必须按出口分开算：
   - 用槽位池时，`status(scope="slot3")` 只数那个出口的 —— 6 个出口
     各用 10 个，全局看是 `60/40`（假超额），按 scope 看每个都是 `10/40`。
   - 老记录（没有 `scope` 字段）属于换出口**之前**的那个 IP，按 scope
     过滤时天然不参与 —— 这是刻意的：换了出口，计数就该从头算。
   - 没有 `scope` 字段的旧记录一律视为 `""`，不需要数据迁移。

为什么用滚动窗口而不是"每天 0 点重置"：
  恢复窗口**未知**（实测等待数分钟未恢复，但没测到确切恢复时间）。
  滚动窗口在恢复时间不确定时更保守，也不会因为跨零点就放行一批。

🔴 **`used` 可以超过 `limit`，等待时间不能按"最早一条滑出"算**（实测踩过）：
   补录会把历史计数一次性推过上限 —— 本项目补录后是 `53/40`。这种状态下
   最早一条滑出窗口只让计数从 53 降到 52，**守卫仍然拦着**；真正要等到第
   `used - limit + 1` 条滑出。旧实现只取 `oldest_ts`，报"169.5 分钟后可再
   注册"，真实值是 **213.7 分钟**，少报 44 分钟且到点后依然拦着。
   判据：`must_expire = max(used - limit + 1, 0)`。

⚠ 这是**本地保护**，不是权威计量：换机器、删文件都会重置。
   它的职责是"别撞墙"，不是"精确对账"。
"""

import json
import os
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import config

_LOCK = threading.Lock()


def state_path() -> Path:
    """计数文件位置。`IR_QUOTA_STATE` 可覆盖（测试隔离 / 多份计数并存）。"""
    override = os.getenv("IR_QUOTA_STATE")
    if override:
        return Path(override).expanduser()
    return (Path(__file__).resolve().parents[1]
            / ".workbuddy-ai" / "state" / "register_quota.jsonl")


def _read_all() -> list[dict]:
    p = state_path()
    if not p.is_file():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue          # 半截行（并发写）直接跳过
            if isinstance(rec, dict) and isinstance(rec.get("ts"), (int, float)):
                out.append(rec)
    except OSError:
        return []
    return out


@dataclass
class QuotaStatus:
    used: int
    limit: int
    window_h: float
    oldest_ts: float | None
    # 窗口内所有记录的时间戳（升序）。`wait_seconds()` 需要它才能在
    # `used > limit` 时算对 —— 只留 `oldest_ts` 会系统性**低估**等待时间。
    ts_sorted: tuple[float, ...] = ()

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    @property
    def must_expire(self) -> int:
        """还要多少条记录滑出窗口，才能再注册一个。

        `check_or_raise` 放行的条件是 `used < limit`（即 `used <= limit-1`），
        所以要滑出 `used - (limit-1)` 条。未触顶时为 0。
        """
        return max(self.used - self.limit + 1, 0)

    def wait_seconds(self) -> float:
        """多久后能再注册**一个**。

        🔴 别写成"最早一条滑出窗口的时间" —— 那只在 `used == limit` 时才对。
        补录（backfill）会把计数推过上限（本项目实测到 53/40），此时最早一条
        滑出后计数只从 53 降到 52，**守卫仍然拦着**。必须等到第
        `must_expire` 条滑出，也就是 `used - limit + 1` 条。

        本项目踩过：旧实现报"169.5 分钟后可再注册"（= 最早一条滑出），
        真实值是 213.7 分钟，**少报 44 分钟**，而且到点后依然是拦着的。
        """
        n = self.must_expire
        if n <= 0:
            return 0.0
        ts = self.ts_sorted
        if len(ts) >= n:
            pivot = ts[n - 1]          # 第 n 早那条；它滑出即可放行
        elif self.oldest_ts is not None:
            pivot = self.oldest_ts     # 兜底：信息不全时宁可保守（偏乐观）
        else:
            return 0.0
        return max(pivot + self.window_h * 3600 - time.time(), 0.0)

    def describe(self) -> str:
        left = f"{self.used}/{self.limit}"
        if self.exhausted:
            w = self.wait_seconds()
            extra = (f"，超额 {self.used - self.limit} 条、需滑出 {self.must_expire} 条"
                     if self.used > self.limit else "")
            return (f"配额已用尽（{left}{extra}；窗口 {self.window_h:g}h），"
                    f"{w / 60:.1f} 分钟后可再注册")
        return f"配额 {left}（窗口 {self.window_h:g}h）"


def status(scope: str = None) -> QuotaStatus:
    """统计**当前滚动窗口内**的成功注册数。

    `scope` 是**出口作用域**（槽位池模式下传 `"slot3"`）。传 `None` = 统计全部。
    🔴 为什么必须有这个参数：服务端的配额是**按出口 IP** 记的，而槽位池里
    每个槽位是一个独立出口 —— 把它们加在一起算会得出一个没有意义的数
    （6 个出口各用 10 个，全局看是 60/40 "超额"，实际上每个出口都还很空）。
    反过来，历史记录（scope 为空）属于**另一个出口**（老的单代理），
    按 scope 过滤时天然不参与 —— 这正是我们要的：换了出口，计数就该从头算。

    没有 `scope` 字段的旧记录一律视为 `""`（向后兼容，不需要迁移）。
    """
    limit = config.REG_QUOTA_MAX
    win_h = config.REG_QUOTA_WINDOW_H
    cutoff = time.time() - win_h * 3600
    recs = [r for r in _read_all() if r["ts"] >= cutoff]
    if scope is not None:
        recs = [r for r in recs if r.get("scope", "") == scope]
    ts = sorted(r["ts"] for r in recs)
    return QuotaStatus(
        used=len(ts),
        limit=limit,
        window_h=win_h,
        oldest_ts=ts[0] if ts else None,
        ts_sorted=tuple(ts),
    )


def record(email: str = "", scope: str = "") -> QuotaStatus:
    """记一次**成功**注册。返回记录后的状态（同 scope）。"""
    p = state_path()
    with _LOCK:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "email": email,
                                    "scope": scope},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass          # 记不上不该让主流程失败
    st = status(scope=scope or None)
    _compact_if_needed(st)
    return st


def _compact_if_needed(st: QuotaStatus) -> None:
    """有效记录远少于总行数时重写文件，避免无限增长。"""
    p = state_path()
    try:
        total = len(_read_all())
    except Exception:
        return
    if total < 200 or st.used > total * 0.5:
        return
    with _LOCK:
        cutoff = time.time() - st.window_h * 3600
        keep = [r for r in _read_all() if r["ts"] >= cutoff]
        try:
            tmp = p.with_suffix(".tmp")
            tmp.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep),
                encoding="utf-8")
            tmp.replace(p)
        except OSError:
            pass


def check_or_raise(*, planned: int = 1, allow_partial: bool = False,
                   scope: str = None) -> QuotaStatus:
    """启动前检查。配额不足时抛 `QuotaExceeded`。

    allow_partial=False（默认，严格）：计划量只要超出剩余就抛。
    allow_partial=True（宽松）：**还有余量**就放行 —— 返回状态，由调用方
        自己把计划量裁到 `st.remaining`；只有余量归零才抛。

    为什么需要宽松档：窗口余量常常"不够全跑但够跑几个"。严格档会让
    "想跑 50 个、只剩 12 个"直接整体拒绝，用户只能去改环境变量；宽松档
    则跑掉那 12 个并把跳过原因讲清楚，行为可预期。

    `scope`：出口作用域，透传给 `status()`（槽位池模式见那边的说明）。
    """
    st = status(scope=scope)
    if st.exhausted or (not allow_partial and st.used + planned > st.limit):
        raise QuotaExceeded(st, planned)
    return st


class QuotaExceeded(RuntimeError):
    def __init__(self, st: QuotaStatus, planned: int):
        self.status = st
        self.planned = planned
        super().__init__(
            f"本地配额保护：本次计划注册 {planned} 个，但{st.describe()}。"
            f"继续跑大概率撞上 B0000（累计配额），且会连累后续单账号也失败。"
        )


# ────────────────────────────────────────────────────────────────
# 补录历史（`python -m src.quota backfill ...`）
# ────────────────────────────────────────────────────────────────
def _parse_created_at(s) -> float | None:
    """`results.json` 里的 `created_at` 是本地时间的 `%Y-%m-%d %H:%M:%S`。"""
    if not s:
        return None
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError, OverflowError):
        return None


def _iter_json_blobs(paths):
    """产出 `(来源标签, 已解析对象)`。支持 `.json` 与 `.zip`（读其中所有 `.json`）。"""
    for raw in paths:
        p = Path(raw)
        if not p.is_file():
            print(f"  跳过（不存在）：{p}")
            continue
        if p.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(p) as z:
                    for n in z.namelist():
                        if not n.lower().endswith(".json"):
                            continue
                        try:
                            yield f"{p.name}:{n}", json.loads(
                                z.read(n).decode("utf-8"))
                        except (ValueError, KeyError, UnicodeDecodeError):
                            continue
            except (zipfile.BadZipFile, OSError) as ex:
                print(f"  跳过（zip 读取失败 {ex}）：{p}")
        else:
            try:
                yield p.name, json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError) as ex:
                print(f"  跳过（解析失败 {ex}）：{p}")


def backfill(paths, *, dry_run: bool = False) -> tuple[int, int]:
    """从历史 `results.json`（或含它的 zip）补录**成功注册**，返回 `(新增, 重复)`。

    为什么需要补录：本地计数是**功能上线之后**才开始记的。此前已经注册过一批
    （换机器、清过 state、或功能刚加），计数会从 0 开始 —— 保护就形同虚设，
    第一次跑必然"先撞墙再停"。补录把这段历史补上。

    判据用 `stages["register"] == "ok"`，**不是** `status == "success"`：
    后者要求整条链路（含登录、建 Key）都跑完，会把"注册成功但登录失败"的
    账号漏掉 —— 而它们**确实占用了注册配额**。用错了会低估消耗。

    去重按 email：同一批文件补录两次不会翻倍。
    时间戳用记录里的 `created_at` 还原，所以滚动窗口判断的是**真实发生时间**，
    而不是补录那一刻 —— 否则一批两小时前的记录会被误算成"刚刚发生"。
    """
    existing = {r.get("email") for r in _read_all()}
    new_recs, added, dup, no_ts = [], 0, 0, 0
    for _src, blob in _iter_json_blobs(paths):
        if not isinstance(blob, list):
            continue
        for rec in blob:
            if not isinstance(rec, dict):
                continue
            if (rec.get("stages") or {}).get("register") != "ok":
                continue
            email = rec.get("email") or ""
            if not email or email in existing:
                dup += 1
                continue
            ts = _parse_created_at(rec.get("created_at"))
            if ts is None:
                no_ts += 1
                continue
            existing.add(email)
            new_recs.append({"ts": ts, "email": email})
            added += 1

    if no_ts:
        print(f"  ⚠ {no_ts} 条缺 created_at，无法还原时间 → 未补录"
              f"（宁可少记，也不能给错时间戳）")

    if not dry_run and new_recs:
        p = state_path()
        with _LOCK:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as f:
                    for r in new_recs:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
            except OSError as ex:
                print(f"  ✗ 写入失败：{ex}")
                return 0, dup
    return added, dup


def _main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "backfill":
        args = argv[1:]
        dry = "--dry-run" in args
        paths = [a for a in args if not a.startswith("--")]
        if not paths:
            print("用法：python -m src.quota backfill <results.json | *.zip>..."
                  " [--dry-run]")
            return 2
        print(f"补录来源 {len(paths)} 个 → state: {state_path()}")
        added, dup = backfill(paths, dry_run=dry)
        print(f"  新增 {added} 条，重复 {dup} 条"
              + ("（--dry-run，未写入）" if dry else ""))
        print(f"  {status().describe()}")
        return 0

    print(f"state : {state_path()}")
    print(f"状态  : {status().describe()}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
