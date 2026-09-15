"""探测 SSO 登录页有没有"直达密码表单"的路由。

动机
----
当前登录流程在进入表单前要走两步点击：

    goto(/login?redirect=...)  →  点「使用手机号 / 密码登录」→ 点「密码登录」tab
    →  等 #normal_login_account 可见

实测这段 `form_ready` 稳定占 **3.3s**（仅次于打字 4.9s 与验证码等待）。
如果 SPA 在点击后会把路由/query 同步到地址栏，就能直接 `goto` 那个 URL，
省掉两次点击与一次重渲染。

本探针**不做登录、不发任何业务请求** —— 只加载登录页、模拟两次点击、
读回 `location.href` 与 DOM 路由线索。风控风险可忽略（等同于访客打开页面）。

用法
----
    python .workbuddy-ai/tmp/probe_login_route.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config  # noqa: E402
from src.browser_login import CHROME_ARGS, build_login_url  # noqa: E402

OUT = Path(__file__).with_name("login_route.json")


def main():
    from playwright.sync_api import sync_playwright

    report = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=config.CHROME_PATH,
                                    headless=True, args=CHROME_ARGS)
        ctx = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai",
                                  viewport=None)
        page = ctx.new_page()

        url = build_login_url()
        report["entry_url"] = url
        t0 = time.time()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        report["goto_ms"] = round((time.time() - t0) * 1000)
        report["href_after_goto"] = page.evaluate("location.href")
        report["hash_after_goto"] = page.evaluate("location.hash")

        # 点击「使用手机号 / 密码登录」
        t1 = time.time()
        try:
            e = page.get_by_text("使用手机号 / 密码登录", exact=False).first
            e.wait_for(state="visible", timeout=15000)
            report["wait_entry_ms"] = round((time.time() - t1) * 1000)
            e.click(timeout=8000)
        except Exception as ex:
            report["entry_click_error"] = f"{type(ex).__name__}: {ex}"[:200]
        page.wait_for_timeout(600)
        report["href_after_entry"] = page.evaluate("location.href")
        report["hash_after_entry"] = page.evaluate("location.hash")
        report["pathname_after_entry"] = page.evaluate("location.pathname")
        report["search_after_entry"] = page.evaluate("location.search")

        # 点击「密码登录」tab
        t2 = time.time()
        try:
            tab = page.get_by_text("密码登录", exact=True).first
            tab.wait_for(state="visible", timeout=15000)
            tab.click(timeout=8000)
        except Exception as ex:
            report["tab_click_error"] = f"{type(ex).__name__}: {ex}"[:200]
        try:
            page.locator("#normal_login_account").wait_for(state="visible",
                                                           timeout=15000)
            report["form_ready_ms"] = round((time.time() - t2) * 1000)
        except Exception as ex:
            report["form_wait_error"] = f"{type(ex).__name__}: {ex}"[:200]
        report["href_after_tab"] = page.evaluate("location.href")
        report["hash_after_tab"] = page.evaluate("location.hash")
        report["pathname_after_tab"] = page.evaluate("location.pathname")

        # 收集页面里的路由线索：react-router 的历史栈、可点元素的 href
        report["route_hints"] = page.evaluate("""() => {
            const out = {};
            out.anchors = Array.from(document.querySelectorAll('a[href]'))
                .map(a => a.getAttribute('href')).filter(Boolean).slice(0, 40);
            out.hasReactRouter = !!(window.history && window.history.state
                && window.history.state.key);
            out.historyState = window.history.state || null;
            out.reactRootKeys = Object.keys(window)
                .filter(k => /react|router|__REACT/i.test(k)).slice(0, 20);
            return out;
        }""")

        # 反向验证：直接用「点完之后的 URL」重开一个 context，看表单是否已在
        ctx2 = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai",
                                   viewport=None)
        p2 = ctx2.new_page()
        cand = report.get("href_after_tab") or ""
        report["candidate_url"] = cand
        if cand and cand != url:
            t3 = time.time()
            try:
                p2.goto(cand, wait_until="domcontentloaded", timeout=60000)
                p2.locator("#normal_login_account").wait_for(state="visible",
                                                             timeout=12000)
                report["direct_hit"] = True
                report["direct_ms"] = round((time.time() - t3) * 1000)
            except Exception as ex:
                report["direct_hit"] = False
                report["direct_error"] = f"{type(ex).__name__}: {ex}"[:200]
                report["direct_ms"] = round((time.time() - t3) * 1000)
        else:
            report["direct_hit"] = None
            report["direct_note"] = "点击未改变 URL —— SPA 用内部状态切 tab，无直达路由"

        ctx.close()
        ctx2.close()
        browser.close()

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n[probe] 已写入 {OUT}")


if __name__ == "__main__":
    main()
