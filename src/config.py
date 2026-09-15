"""全局配置。

所有密钥类信息集中在此，可通过环境变量或项目根的 `.env` 文件覆盖。

🔴 凭据**不写死在代码里** —— 本项目托管在公开仓库，写死等于直接泄漏。
   首次使用：`cp .env.example .env`，填入真实值。`.env` 已在 .gitignore 中。
"""

import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """极简 `.env` 解析（stdlib 实现，不引 python-dotenv 依赖）。

    只填充「尚未存在于 os.environ」的键 —— 真实环境变量优先级更高，
    这样临时覆盖（`IR_XXX=1 python run.py`）依然生效。
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# ── CF Worker 临时邮箱 ────────────────────────────────────────────
WORKER_BASE = os.getenv("IR_WORKER_BASE", "https://temp-email-worker.zhuhaoyi181.workers.dev")
# ⚠ 默认留空。缺它时由 `validate()` 在入口处报错，而不是静默发一堆 401。
WORKER_ADMIN_TOKEN = os.getenv("IR_WORKER_ADMIN_TOKEN", "")
WORKER_DOMAIN = os.getenv("IR_WORKER_DOMAIN", "liziai.cloud")

# ── OpenXLab SSO ─────────────────────────────────────────────────
SSO_BASE = "https://sso.openxlab.org.cn"
SSO_GW = f"{SSO_BASE}/gw/uaa-be/api/v1"

# 注册时使用的应用身份（来自 discovery 活动页）
CLIENT_ID = "dagw07mkg1bazlxzoy31"
SOURCE = "discovery"

# RSA 公钥（SPKI/DER base64，服务端静态）
SSO_PUBKEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCOst3X5k3uqRpKtFOfLQdh5ZyakdP0fnP6CyPs"
    "9e2BWF/Jud+BZNNWOPtm5roUu3Cf0wFbvha4uD+XxmNz/3Ea+VOrfbhIeSWX3CTZ+9oAWERz0ftF"
    "oEYTf2nAt5LORhhNHt2Wea8yMTD8GoZ/asm2GX3B/CjIa6PwVlbRHX9/bwIDAQAB"
)

# ── Discovery 平台（API Key 与额度）────────────────────────────────
DISCOVERY_BASE = "https://discovery.intern-ai.org.cn"
DISCOVERY_API = f"{DISCOVERY_BASE}/api"

# ── API Key 专用推理网关 ──────────────────────────────────────────
# 🔴 别用 chat.intern-ai.org.cn —— 那是网页版聊天后端，只认 SSO JWT，
#    且要求账号绑定手机号（-20035），拿 sk- key 打会得到 401 A0211。
#    真正接受 sk- key 的是下面这个主机，路径前缀 /v1（OpenAI 兼容）。
CHAT_API_BASE = os.getenv(
    "IR_CHAT_API_BASE", "https://discovery-api.intern-ai.org.cn/v1"
)

# TokenPlan 可用模型（2026-09-15 实测，来自 GET /v1/models）
# ⚠ intern-s1 不在其中，用它会得到 model_not_available
CHAT_MODELS = [
    "deepseek-v4-flash-0731",
    "minimax-m3",
    "deepseek-v4-flash-vision",
    "qwen3.8-27b",
    "intern-s2",
    "deepseek-v4-pro-0813",
    "Agents-A1",
    "Atria-Dawn-Preview",
    "glm-5.3",
    "kimi-k2.6",
]

# 活动邀请信息（注册跳转链接中的参数）
INVITER_USER_ID = "415100755"
INVITER_USERNAME = "OpenXLab-ymAqeOKDn"
ACTIVITY_PATH = "activity/reasearch-acceleration-camp"

# ── 行为参数 ──────────────────────────────────────────────────────
# 邮件实测在注册后 3 秒内到达；轮询间隔 0.8s 可在 1~2 次内命中，
# 而 /admin/all 单次往返 ~600ms，再密就只是在打 Cloudflare。
MAIL_POLL_INTERVAL = 0.8    # 秒
MAIL_POLL_TIMEOUT = 120     # 秒
# /admin/all 返回是「新→旧」排序，所以只取最近 N 条即可命中刚到的激活信。
#
# 🔴 更正（2026-09-15 重测）：早先这里写着"延迟与 limit 基本无关（~550~820ms，
#    瓶颈在往返）"—— **那是错的**。重新逐档实测：
#
#      limit   耗时    体积
#        50   568ms   57 KB
#        5    265ms   5.8 KB
#        1    269ms   1.2 KB
#
#    延迟明显随体积增长（50→5 直接减半），只有 ~265ms 是真正的往返底座。
#    另外该接口**不支持按收件人过滤**（email/to/to_address 参数全被忽略，
#    返回体逐字节相同），只能整表拉回来自己筛。
#    → 所以轮询用自适应窗口：从 MAIL_LIST_MIN 起步、未命中翻倍、上限 MAIL_LIST_LIMIT。
#      见 tempmail.wait_for_mail 的 docstring。
MAIL_LIST_MIN = 5           # 轮询起始窗口（快路径）
MAIL_LIST_LIMIT = 50        # 窗口上限（兜底，也是手动调 list_mails 的默认值）
REQUEST_TIMEOUT = 30        # 秒

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

# 本机 Chrome（Playwright 驱动，避免下载额外浏览器）
CHROME_PATH = os.getenv(
    "IR_CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)


# ── 启动校验 ──────────────────────────────────────────────────────
def validate(*, need_worker_token: bool = True) -> list[str]:
    """返回缺失的必需配置项（空列表 = 就绪）。

    刻意**不在 import 时抛错** —— 那样连 `--help` 和离线分析都跑不起来。
    由入口显式调用，报错时直接给出修法。
    """
    missing = []
    if need_worker_token and not WORKER_ADMIN_TOKEN:
        missing.append("IR_WORKER_ADMIN_TOKEN")
    return missing

