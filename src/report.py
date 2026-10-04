"""报告渲染层：跑完一批之后印给人看的那一大块 stdout。

为什么单独一个模块
==================
`run.py` 一旦被 import 就会把整套 pipeline 拉进 `sys.path`（见
`tests/test_dependency_surface.py` 的元测试），所以**测试链不该 import 它**。
凡是需要被测试钉住的格式/判据，一律抽到这里来。

本模块**只依赖 stdlib**（`statistics`），刻意**不 import
`src.pipeline` / `common.config` / `src.quota`**：

  * `error_kind_of` / `ERR_QUOTA` / `config` / `quota` 全部由调用方**注入** ——
    报告层因此是依赖图上的叶子，不会把 `requests` 之类的传递依赖带进测试链
    （`src.pipeline` → `src.discovery` → `requests`，那条链已经在 CI 上红过）；
  * 测试里想造「配额触顶」「某个出口没额度」这类场景，塞个桩就行，
    不必 monkeypatch 真实模块。

🔴 `render_batch_report()` 的 stdout 是**逐字节契约**。
   2026-09-20 把它从 `run.py` 抽出来时，基准不是手抄的，而是拿改前 `run.py`
   的同一段源码**原样执行**产生的输出（3 个场景：全分支 / 最小 / 空），
   改完逐字节比对。动这里任何一行都要重新走一遍这个比对。
"""

import statistics

# 标签与取值**必须同源**。两处各写一份，就会漂移成「四个标签配三个值」。
LATENCY_LABELS = "均值 / 中位 / 最快 / 最慢"


def latency_summary(totals) -> str:
    """把一组耗时（秒）格式化成 `均值 / 中位 / 最快 / 最慢` 一行。

    🔴 四个标签必须配**四个值，而且印在同一行**。

    2026-09-20 实测到的缺陷：调用方把「均值」单独留在上一行的合计列，
    这一行仍然写四个标签却只给三个值 ⇒ 读者按左对齐会把 **39.1（真正的
    最慢）读成「最快」**，而「最慢」看着像缺失。
    该批合计列按原始表格复算 = 均值 23.5 / 中位 22.6 / 最快 20.0 / 最慢 39.1，
    与打印出来的三个数字逐一对上 —— 错的不是数字，是标签的位置。

    空输入返回 `""`（印不印由调用方决定）。
    """
    xs = [float(x) for x in totals]
    if not xs:
        return ""
    return (f"{statistics.mean(xs):.1f} / {statistics.median(xs):.1f} / "
            f"{min(xs):.1f} / {max(xs):.1f}")


def fmt_ms(v) -> str:
    """毫秒 → 一位小数的秒；**非数字给 `-`**。

    非数字（缺段：失败 / 跳过 / 还没走到那一段）必须区别于 `0.0` ——
    把半截耗时和完整耗时放在同一列里比是误导。
    """
    return f"{v / 1000:.1f}" if isinstance(v, (int, float)) else "-"


def render_batch_report(results, written, wall, *, workers, quota, config,
                        error_kind_of, err_quota) -> None:
    """渲染一批结果的收尾报告（直接 `print` 到 stdout）。

    `results`   —— `AccountRecord` 列表（成功 / 跳过 / 失败都在里面）
    `written`   —— 结果落盘路径（会 `.resolve()` 后印出来）
    `wall`      —— 批次墙钟秒数（只在没有 `keys_done`/`batch_total` 时兜底）
    `workers`   —— 浏览器并发数（只用来算「最后一轮有没有 worker 空转」）
    `quota`     —— 有 `status()` 的对象（`src.quota` 或桩）
    `config`    —— 有 `CHAT_API_BASE` / `CHAT_MODELS` 的对象
    `error_kind_of` —— `src.pipeline.error_kind_of`
    `err_quota`     —— `src.pipeline.ERR_QUOTA`

    ⚠ 判「配额触顶」走 `error_kind_of()`（**结构化字段**），不搜 `error` 文本 ——
      我们自己拼的守卫文案里也含 `B0000`，文本匹配会把主动中止算进来。
    """
    ok = [r for r in results if r.status == "success"]
    skipped = [r for r in results if r.status == "skipped"]
    bad = [r for r in results if r.status not in ("success", "skipped")]

    print(f"\n{'=' * 72}")
    print(f"DONE: {len(ok)}/{len(results)} succeeded -> {written.resolve()}")
    if skipped:
        print(f"      {len(skipped)} 个被配额保护跳过（未发请求，非失败）")

    # 耗时明细：注册 / 登录 / 建Key 三段，定位瓶颈用
    # 🔴 「合计」= 注册+登录+建Key，是**单账号端到端**耗时，必须自己算。
    #    别去读 `timings['total']` —— 那个键**根本不存在**，
    #    于是老代码 `tm.get('total') or tm.get('batch_total')` 会静默退化成
    #    `batch_total`（批次墙钟），导致这一列**每行都是同一个数**，
    #    看起来像"所有账号耗时一样"，其实是显示 bug（实测 510.0 刷满 39 行）。
    #    批次墙钟是**批次级**指标，跟单账号耗时不是一个东西，不能混进这一列。
    print("\n耗时明细（秒）：")
    print(f"  {'email':40s} {'注册':>7s} {'登录':>7s} {'建Key':>7s} {'合计':>7s}")
    totals: list[float] = []
    for r in results:
        tm = r.timings or {}
        stages = [tm.get(k) for k in ("register", "login", "key")]
        # 三段齐了才算"端到端合计"；缺段（失败/跳过）给 "-"，
        # 否则会把半截耗时和完整耗时放在一列里比，是误导。
        # ⚠ 先筛成 `nums` 再比长度，而不是 `all(...)` + `sum(stages)`：
        #   两者判据**完全等价**（数值项数相等 ⇔ 全部都是数值），但 `all()`
        #   不能把 `stages` 的元素类型收窄成数值 —— 静态检查器会在 `sum()`
        #   处报 `list[Unknown | None]`。
        nums = [v for v in stages if isinstance(v, (int, float))]
        if len(nums) == len(stages):
            total_ms = sum(nums)
            totals.append(total_ms / 1000)
            total_s = f"{total_ms / 1000:.1f}"
        else:
            total_s = "-"
        print(f"  {(r.email or '(未建邮箱)'):40s} "
              f"{fmt_ms(tm.get('register')):>7s} "
              f"{fmt_ms(tm.get('login')):>7s} "
              f"{fmt_ms(tm.get('key')):>7s} "
              f"{total_s:>7s}")
    if totals:
        # 表头单独取变量：原来写成 `{'…%d…' % len(totals):40s}` 嵌在 f-string 里，
        # 两种插值语法叠在一起，UP031 会报。提取后只剩一种。
        _hdr = f"── 统计（{len(totals)} 个完整样本）"
        print(f"  {_hdr:40s}")
        # 🔴 四个标签和四个取值**必须在同一个 print 里**。
        #    原来把「均值」单独留在上面那行的合计列，这一行仍写四个标签却只给
        #    三个值 ⇒ 读者按左对齐会把 39.1（真正的**最慢**）读成「最快」。
        #    （2026-09-20 复跑时按日志原始表格复算才发现：数字没错，是标签错位。）
        print(f"  {'   ' + LATENCY_LABELS:40s} "
              f"{'':>7s} {'':>7s} {'':>7s} "
              f"{latency_summary(totals)}")

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
              f"登录 {fmt_ms(slow.timings.get('login'))}s）：")
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
              f"注册 {fmt_ms(slow_reg.timings.get('register'))}s）：")
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
            print("  ⚠ 极差超过最快值的 1.5 倍 —— 总时长多半由这个离群值决定，"
                  "不是均值")

    if results:
        tm0 = results[0].timings or {}
        kd, bt = tm0.get("keys_done"), tm0.get("batch_total")
        print(f"\n吞吐（{len(results)} 个账号，workers={workers}）：")
        if kd and bt:
            print(f"  关键路径（全部 key 建出）: {kd / 1000:6.1f}s "
                  f"= {kd / 1000 / len(results):5.1f}s / 账号")
            print(f"  含末尾统一校验          : {bt / 1000:6.1f}s "
                  f"= {bt / 1000 / len(results):5.1f}s / 账号")
            rounds = -(-len(results) // workers)     # ceil
            if rounds * workers != len(results):
                print(f"  ⚠ {len(results)} 个账号 / {workers} 路 = {rounds} 轮，"
                      f"最后一轮有 worker 空转。"
                      f"凑成 {rounds * workers} 个能摊得更薄。")
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
        #
        # 🔴 判据走 `error_kind_of()`（结构化字段），不再搜 error 文本 ——
        #    我们自己拼的守卫文案里也含 `B0000`，文本匹配会把主动中止算进来。
        #    这里 `bad` 已经排除了 skipped，所以两种判据当前等价；改成字段
        #    是为了让"以后新增一条含 B0000 的文案"不会悄悄改掉这个数字。
        quota_hits = [r for r in bad if error_kind_of(r) == err_quota]
        if quota_hits:
            print(f"\n  ⚠ 其中 {len(quota_hits)} 个是注册配额触顶（B0000 请求频繁）")
            print("    这是**累计量**限制，不是瞬时速率 —— 调 REG_MIN_INTERVAL 无效。")
            print("    实测：同一时段累计约 40 个账号后触发，需等待窗口恢复后再跑。")
            print("    → 建议把 IR_REG_QUOTA_MAX 下调到本次触顶点，让本地保护更早拦住。")

    if ok:
        print("\n调用方式（OpenAI 兼容）：")
        print(f"  base_url = {config.CHAT_API_BASE}")
        print(f"  model    = {config.CHAT_MODELS[0]}")
        print(f"  api_key  = {ok[0].api_key}")
        print("  ⚠ 不要用 chat.intern-ai.org.cn（那是网页版，要绑手机号）")

    # 收尾再报一次配额，让"本次消耗了几个"一眼可见。
    qs2 = quota.status()
    print(f"\n本地配额：{qs2.describe()}（本次成功 {len(ok)} 个，"
          f"跳过 {len(skipped)} 个）")
    print("=" * 72)
