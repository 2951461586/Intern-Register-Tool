"""验证码交互原语 —— 勾选框点击与滑块探测。

⚠ 必须点 `#aliyunCaptcha-checkbox-icon`（20x20 真实图标），
  点外层 wrapper / body 都无效且**不报错**。
"""

import random

from .behavior import _human_move


def _click_checkbox(page, cur: tuple) -> tuple:
    """移动到 `#aliyunCaptcha-checkbox-icon` 中心并按下。

    返回 (新光标位置, 是否成功发出点击)。
    """
    icon = page.locator("#aliyunCaptcha-checkbox-icon").first
    box = icon.bounding_box()
    if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
        return cur, False
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2

    # 先移到附近（带过冲），停顿，再校正到中心 —— 人类常见动作模式
    nx = cx + random.uniform(-45, 45)
    ny = cy + random.uniform(-30, 30)
    _human_move(page, cur[0], cur[1], nx, ny)
    page.wait_for_timeout(random.randint(150, 380))
    _human_move(page, nx, ny, cx, cy, steps=random.randint(6, 14))
    page.wait_for_timeout(random.randint(90, 260))

    page.mouse.down()
    page.wait_for_timeout(random.randint(70, 170))
    page.mouse.up()
    return (cx, cy), True


def _has_slider(page) -> str:
    """检测点击后是否弹出了滑块 / 拼图二次验证。"""
    try:
        return page.evaluate("""() => {
            const ids = ['aliyunCaptcha-sliding', 'aliyunCaptcha-puzzle',
                         'aliyunCaptcha-slider', 'aliyunCaptcha-slide'];
            for (const i of ids) {
                const el = document.getElementById(i);
                if (el && el.getBoundingClientRect().width > 0) return i;
            }
            const all = document.querySelectorAll('[id*="Captcha"]');
            for (const el of all) {
                const r = el.getBoundingClientRect();
                if (r.width > 100 && /slid|puzzle|slide/i.test(el.id)) return el.id;
            }
            return '';
        }""")
    except Exception:
        return ""
