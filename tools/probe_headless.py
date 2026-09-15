"""探针：实测无头浏览器能否通过阿里云验证码。

背景：此前"headless 会被识别"是**推断**，从未在当前实现
（最小化注入 + 人类鼠标轨迹）下实测过。这里做定量测试。

测三组配置，每组单次尝试：
  H1  headless=True，沿用现有 CHROME_ARGS
  H2  headless=True + 显式窗口尺寸（headless=new 下窗口指标才合理）
  H3  headless=True + 软件 GPU（SwiftShader），补齐 WebGL renderer

同时 dump 每组的环境指纹，看哪些字段在无头下异常。
"""
import io
import json
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config
from src.pipeline import gen_password, gen_username
from src.sso import SSOClient
from src.tempmail import TempMailClient

CHROME = config.CHROME_PATH
BASE_ARGS = ["--disable-blink-features=AutomationControlled", "--no-sandbox",
             "--disable-features=IsolateOrigins,site-per-process"]

ENV_PROBE = """() => {
  const gl = document.createElement('canvas').getContext('webgl');
  let renderer = null, vendor = null;
  if (gl) {
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    if (dbg) { renderer = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL);
               vendor = gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL); }
  }
  return {
    webdriver: navigator.webdriver,
    ua: navigator.userAgent,
    inner: [innerWidth, innerHeight],
    outer: [outerWidth, outerHeight],
    screen: [screen.width, screen.height],
    availScreen: [screen.availWidth, screen.availHeight],
    dpr: devicePixelRatio,
    plugins: navigator.plugins.length,
    languages: navigator.languages,
    chrome: typeof window.chrome,
    hc: navigator.hardwareConcurrency,
    glRenderer: renderer, glVendor: vendor,
    cdc: Object.keys(window).filter(k => /cdc|selenium|webdriver|driver/i.test(k)),
  };
}"""


def probe_and_login(label, extra_args, account, password):
    from playwright.sync_api import sync_playwright

    print(f"\n{'=' * 70}\n### {label}\n{'=' * 70}", flush=True)
    print(f"  额外参数: {extra_args or '(无)'}", flush=True)

    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, headless=True,
                              args=BASE_ARGS + extra_args)
        ctx = b.new_context(locale="zh-CN", timezone_id="Asia/Shanghai",
                            viewport=None, color_scheme="light")
        ctx.add_init_script("if (!window.chrome) { window.chrome = {runtime: {}}; }")
        pg = ctx.new_page()
        pg.goto("about:blank")
        info = pg.evaluate(ENV_PROBE)
        print("  指纹:", json.dumps(info, ensure_ascii=False), flush=True)
        b.close()

    # 真正的登录尝试（走正式实现，headless=True）
    import src.browser_login as bl

    orig = bl.CHROME_ARGS
    bl.CHROME_ARGS = BASE_ARGS + extra_args
    try:
        t0 = time.time()
        res = bl.login(account, password, headless=True, timeout=90,
                       attempts=1, verbose=True)
        dt = time.time() - t0
        print(f"  >>> 结果 ok={res.ok} 耗时={dt:.0f}s reason={res.reason}", flush=True)
        print(f"  >>> captcha={res.captcha_stage.get('verify')}", flush=True)
        return res.ok
    finally:
        bl.CHROME_ARGS = orig


def main():
    mail, sso = TempMailClient(), SSOClient()
    email = mail.create_mailbox(count=1)[0]
    user, pwd = gen_username(), gen_password()
    reg = sso.register(user, email, pwd)
    print(f"账号: {email} uid={reg.sso_uid} ok={reg.ok}", flush=True)
    link = mail.wait_for_activation_link(email)
    print(f"激活: {sso.activate_from_url(link)}", flush=True)

    results = {}
    results["H1 基础无头"] = probe_and_login("H1 基础无头", [], email, pwd)
    time.sleep(20)
    results["H2 显式窗口尺寸"] = probe_and_login(
        "H2 显式窗口尺寸", ["--window-size=1280,720", "--force-device-scale-factor=1"],
        email, pwd)
    time.sleep(20)
    results["H3 软件 GPU"] = probe_and_login(
        "H3 软件 GPU",
        ["--window-size=1280,720", "--force-device-scale-factor=1",
         "--use-gl=angle", "--use-angle=swiftshader"],
        email, pwd)

    print(f"\n{'=' * 70}\n汇总\n{'=' * 70}")
    for k, v in results.items():
        print(f"  {'✅ 通过' if v else '❌ 失败'}  {k}")


if __name__ == "__main__":
    main()
