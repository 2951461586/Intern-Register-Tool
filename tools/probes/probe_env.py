"""探针：对比 viewport 覆盖 vs --window-size 两种模式下浏览器指纹的一致性。

核心疑问：Playwright 的 new_context(viewport=...) 通过 CDP
Emulation.setDeviceMetricsOverride 只改 innerWidth/innerHeight，
改不了 outerWidth/outerHeight。若 inner > outer，真实浏览器不可能出现，
属于硬性自动化特征。
"""
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

PROBE = """() => {
  const uad = navigator.userAgentData || null;
  return {
    webdriver: navigator.webdriver,
    ua: navigator.userAgent,
    uaDataBrands: uad ? JSON.stringify(uad.brands) : null,
    uaDataMobile: uad ? uad.mobile : null,
    uaDataPlatform: uad ? uad.platform : null,
    platform: navigator.platform,
    inner: [window.innerWidth, window.innerHeight],
    outer: [window.outerWidth, window.outerHeight],
    screen: [screen.width, screen.height],
    availScreen: [screen.availWidth, screen.availHeight],
    dpr: window.devicePixelRatio,
    colorDepth: screen.colorDepth,
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory,
    plugins: navigator.plugins.length,
    languages: navigator.languages,
    chrome: typeof window.chrome,
    notif: typeof window.Notification,
    maxTouch: navigator.maxTouchPoints,
    hasCdc: Object.keys(window).filter(k => /cdc|selenium|webdriver|driver/i.test(k)),
  };
}"""


def run(label, *, use_viewport):
    with sync_playwright() as p:
        args = ["--disable-blink-features=AutomationControlled", "--no-sandbox",
                "--disable-features=IsolateOrigins,site-per-process"]
        if not use_viewport:
            args.append("--window-size=1920,1080")
            args.append("--window-position=0,0")
        b = p.chromium.launch(executable_path=CHROME, headless=False, args=args)
        kw = {}
        if use_viewport:
            kw = {"viewport": {"width": 1920, "height": 1080},
                  "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/152.0.0.0 Safari/537.36")}
        else:
            kw = {"viewport": None}
        ctx = b.new_context(locale="zh-CN", **kw)
        pg = ctx.new_page()
        pg.goto("about:blank")
        info = pg.evaluate(PROBE)
        print(f"===== {label} =====")
        print(json.dumps(info, ensure_ascii=False, indent=2))
        b.close()


if __name__ == "__main__":
    run("A: viewport 覆盖 (当前实现)", use_viewport=True)
    print()
    run("B: --window-size，不覆盖 viewport", use_viewport=False)
