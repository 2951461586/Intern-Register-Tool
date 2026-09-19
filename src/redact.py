"""脱敏助手 —— **日志 / 输出边界必须过这里**。

为什么单独成模块
----------------
这两个函数原来住在 `src/config.py` 里，但它们跟"配置"没有任何关系：
一个读环境变量，另一个只做字符串裁剪。混在一起会让调用方以为
"读配置"和"打日志"是同一件事，从而在新增输出点时漏掉脱敏。
（本项目真的漏过一次：`tools/probes/probe_proxy.py` 原来会打印 `IR_PROXY=<完整串>`。）

🔴 为什么脱敏是硬要求：代理串是 `scheme://user:pass@host:port` 形式，
   直接 print / 写日志会把**账密**一起落盘。而 `.workbuddy-ai/tmp/*.log`
   经常被贴进 issue 或对话里 —— 泄漏一次就够。

用法：**凡是把配置值写进 stdout / 日志 / 报告的地方，先过这两个函数。**
约定与例外见 `docs/security-conventions.md`。
"""


def redact_url(url: str) -> str:
    """把 URL 的 userinfo（`user:pass@`）换成 `***`，其余保留。

        http://user:pass@203.0.113.30:8080  →  http://***@203.0.113.30:8080
        http://127.0.0.1:7901               →  原样（没有 userinfo）
    """
    if not url or "://" not in url:
        return url or ""
    scheme, _, rest = url.partition("://")
    if "@" not in rest:
        return url
    _userinfo, _, hostpart = rest.rpartition("@")
    return f"{scheme}://***@{hostpart}"


def redact(raw: str, keep: int = 0) -> str:
    """把凭据串打成 `<N chars>` —— **连前缀都不打**。

    `keep>0` 只用于本地排查。默认 0，因为"前 6 位"这类信息
    也足以在别处做关联（见 docs/security-conventions.md 报告规范）。
    """
    if not raw:
        return ""
    if keep and len(raw) > keep:
        return f"{raw[:keep]}…<{len(raw)} chars>"
    return f"<{len(raw)} chars>"
