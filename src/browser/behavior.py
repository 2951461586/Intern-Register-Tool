"""人类化鼠标行为 —— 阿里云行为风控真正评估的信号。

风控看的是**轨迹与按键节奏**，所以这里的每个随机量都是功能的一部分，
不是"随便加点随机"。实测依据见 `__init__.py` 包 docstring 的
「行为风控：F001 的真正判据」与「验证码有两条通路」两节。

⚠ 不要为了"提速"去掉步进延时或过冲回正 —— 那会直接导致 F001 拒绝。
"""

import math
import random

from .constants import MICRO_MOVE, MICRO_WAIT_MS


# ────────────────────────────────────────────────────────────────
# 人类化鼠标动作
# ────────────────────────────────────────────────────────────────
def _human_move(page, x0: float, y0: float, x1: float, y1: float,
                *, steps: int = None) -> None:
    """沿三次贝塞尔曲线分步移动鼠标，模拟人类轨迹。

    真人移动的特征：不是直线、有轻微弧度、速度先快后慢（ease-out）、
    步间间隔不均匀。这里用 smoothstep 缓动 + 随机控制点偏移还原。

    ⚠ 步间延时（9~30ms）**不要去掉** —— 它是被风控评估的信号本身。
    """
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    if dist < 1.5:
        page.mouse.move(x1, y1)
        return
    if steps is None:
        steps = max(10, min(70, int(dist / 7)))
    amp = min(55.0, dist * 0.18)
    c1x = x0 + dx * 0.30 + random.uniform(-amp, amp)
    c1y = y0 + dy * 0.30 + random.uniform(-amp, amp)
    c2x = x0 + dx * 0.70 + random.uniform(-amp, amp)
    c2y = y0 + dy * 0.70 + random.uniform(-amp, amp)
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)          # smoothstep 缓动
        mt = 1 - e
        x = (mt ** 3 * x0 + 3 * mt * mt * e * c1x
             + 3 * mt * e * e * c2x + e ** 3 * x1)
        y = (mt ** 3 * y0 + 3 * mt * mt * e * c1y
             + 3 * mt * e * e * c2y + e ** 3 * y1)
        page.mouse.move(x, y)
        page.wait_for_timeout(random.randint(9, 30))


def _idle_wait(page, *, stats: dict = None) -> None:
    """**不发任何输入事件**，只泵送事件循环地等一小段。

    两个用途，语义相同（都是"让页面安静下来"）：
      1. 对照组（IR_NO_MICRO_MOVE=1）—— 隔离出"鼠标事件"这一个变量；
      2. 超过 `MICRO_BUDGET_S` 之后的安静期 —— 停止喂数据，
         给 SDK 一个不再被新轨迹延后的窗口去下降级决定。

    ⚠ 仍然必须用 `wait_for_timeout` 而不是 `time.sleep`（见文件头第 3 条）：
      安静期里恰恰最需要网络事件回调被执行。
    """
    if stats is not None:
        stats["idle_waits"] = stats.get("idle_waits", 0) + 1
    page.wait_for_timeout(MICRO_WAIT_MS)


def _micro_move(page, cur: tuple, vw: int, vh: int, *, stats: dict = None) -> tuple:
    """做一次小幅鼠标移动，返回新位置。

    用途：在"等验证码 SDK 初始化"这类原本空转的等待里持续产生行为数据。
    每次约 150~350ms，比整段 warmup 更贴近真人（真人不会停手不动）。

    对照组（IR_NO_MICRO_MOVE=1）：只等同样长的时间，**不发鼠标事件**。
    这是为了回答一个具体问题 —— 微移动到底加快了还是拖慢了 SDK 的
    TRACELESS→CHECK_BOX 降级时序。

    统计口径（`stats`）刻意把三种动作分开计数，否则"移动次数"会把
    纯等待也算进去，实验组和对照组就没法比：
      moves      真正发出了鼠标轨迹的微移动次数
      points     累计发出的轨迹点数
      idle_waits 未发事件的纯等待次数（对照组 + 超预算后的安静期）
    """
    if not MICRO_MOVE:
        _idle_wait(page, stats=stats)
        return cur
    x = min(max(cur[0] + random.uniform(-170, 170), 20), max(vw - 20, 21))
    y = min(max(cur[1] + random.uniform(-120, 120), 20), max(vh - 20, 21))
    steps = random.randint(5, 13)
    _human_move(page, cur[0], cur[1], x, y, steps=steps)
    if stats is not None:
        stats["moves"] = stats.get("moves", 0) + 1
        stats["points"] = stats.get("points", 0) + steps
    page.wait_for_timeout(random.randint(50, 150))
    return (x, y)


def _warmup_mouse(page, vw: int, vh: int, *, stats: dict = None) -> tuple:
    """提交前的短暖场（2~3 轮）。

    真正的长时间鼠标活动交给 `_micro_move` 在验证码初始化等待期间做 ——
    那样不额外占用时间。这里只保证"提交动作前手是动过的"。
    """
    x = random.uniform(vw * 0.25, vw * 0.75)
    y = random.uniform(vh * 0.25, vh * 0.60)
    page.mouse.move(x, y)
    page.wait_for_timeout(random.randint(120, 260))
    for _ in range(random.randint(2, 3)):
        x, y = _micro_move(page, (x, y), vw, vh, stats=stats)
    return x, y
