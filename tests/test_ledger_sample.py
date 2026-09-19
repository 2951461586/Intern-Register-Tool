"""脱敏样本台账（`tests/fixtures/ledger_sample.json`）自己的守卫。

为什么需要这个文件
------------------
`results.json` 含明文账号 / 密码 / JWT / API Key，被 `.gitignore` 排除，
**不在仓库里** ⇒ CI 上读不到 ⇒ 2026-09-19 CI 第二次变红。

修法不是"CI 上跳过就完事"（那样真实形状的回归在 CI 上等于没有），
而是入库一份**形状保真、值全编造**的样本，让 `any_ledger` 夹具
在任何环境都能拿到一本"长得像真的"台账。

但样本一旦入库就会**漂移**：有人为了省事删掉几档形状、或者把基线缩到 10 条以下，
那些依赖它的用例会以各种奇怪的方式失效（`StopIteration` / `DID NOT RAISE`），
而报错里看不出"其实是样本不合格"。所以样本的**形状覆盖**必须被钉住 ——
这个文件就是那颗钉子。

⚠ 这里是**唯一**允许出现"台账形状清单"的地方：改样本先看这里。
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests" / "fixtures" / "ledger_sample.json"


@pytest.fixture(scope="module")
def sample() -> list[dict]:
    assert SAMPLE.is_file(), f"样本台账不存在：{SAMPLE}"
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════════
# 一、样本必须是一本**能用**的台账
# ═══════════════════════════════════════════════════════════════════
def test_sample_is_a_list_of_dicts(sample):
    assert isinstance(sample, list), type(sample).__name__
    assert sample, "样本是空的 —— 那就退化成了 CI 上最初的 `[]` 问题"
    assert all(isinstance(r, dict) for r in sample)


def test_sample_is_long_enough_for_the_shrinkage_guard(sample):
    """T7 拿 `any_ledger[:10]` 去撞"防缩水"护栏，所以样本必须**明显长于 10 条**。

    这里刻意不写 `>= 11`（刚好够）而是留出余量：T7 的语义是"截断后条数变少"，
    只有 11 条时任何一次样本瘦身都会立刻把它打回 `DID NOT RAISE`。
    """
    assert len(sample) > 10, f"样本只有 {len(sample)} 条，T7 截不出缩水"


def test_sample_emails_are_unique_like_the_real_ledger(sample):
    """样本的 email 必须唯一 —— 这是**行为一致性**要求，不是洁癖。

    T1 断言 `kept == base`、T2 断言 `len(m2) == len(real)`，两者都隐含
    "`merge_records` 不会因为去重而减少条数"。真实台账满足这一点
    （实测 277 条 / 217 个 email 全唯一），所以样本也必须满足 ——
    否则同一份用例在样本上过、在真实台账上红（或反过来），
    样本就**失去了替代真实数据的资格**。
    """
    emails = [r["email"] for r in sample if r.get("email")]
    dupes = sorted({e for e in emails if emails.count(e) > 1})
    assert not dupes, f"样本里有重复 email：{dupes}"


# ═══════════════════════════════════════════════════════════════════
# 二、形状覆盖 —— 逐档对应真实台账实测到的形态
# ═══════════════════════════════════════════════════════════════════
# 右侧的数字来自 2026-09-19 对真实 `results.json`（277 条）的实测统计。
REQUIRED_SHAPES = [
    ("success 记录（T2 的前提）",
     lambda r: r.get("status") == "success"),
    ("failed 记录",
     lambda r: r.get("status") == "failed"),
    ("skipped 记录",
     lambda r: r.get("status") == "skipped"),
    ("无 status 键的记录（`export_keys` 导出行，真实台账 38 条）",
     lambda r: "status" not in r),
    ("email 为空串的记录（配额拦截占位形态，真实台账 60 条）",
     lambda r: "email" in r and not r["email"]),
    ("完整记录（>= 33 键；真实台账 success 里 99 条是这个形状）",
     lambda r: len(r) >= 33),
    ("精简记录（<= 14 键；真实台账 skipped/failed 的基础形态）",
     lambda r: len(r) <= 14),
    ("导出行（无 status 且 <= 10 键；真实台账 38 条全是 9 键）",
     lambda r: "status" not in r and len(r) <= 10),
    ("含未知字段的记录（版本前向兼容：老代码不该被新字段吓到）",
     lambda r: any(k not in _KNOWN_KEYS for k in r)),
    ("数值 0（`0` 不是空值 —— `merge_fragments` 的 `_EMPTY` 边界）",
     lambda r: any(v == 0 and isinstance(v, int) and not isinstance(v, bool)
                   for v in r.values())),
    ("布尔 False（同上：`False` 不是空值）",
     lambda r: any(v is False for v in r.values())),
    ("None（`None` 是**合法值**，与「没这个键」不同）",
     lambda r: any(v is None for v in r.values())),
    ("空串（空值的一种，不许覆盖非空值）",
     lambda r: any(v == "" for v in r.values())),
    ("空 dict 值",
     lambda r: any(v == {} and isinstance(v, dict) for v in r.values())),
    ("空 list 值",
     lambda r: any(v == [] and isinstance(v, list) for v in r.values())),
    ("嵌套 dict 值（`stages` / `timings` / `balance_raw` / `verify_usage`）",
     lambda r: any(isinstance(v, dict) and v for v in r.values())),
    ("嵌套 list 值（`key_names`）",
     lambda r: any(isinstance(v, list) and v for v in r.values())),
    ("非 ASCII 值（编码假设：读写不依赖 `ensure_ascii`）",
     lambda r: any(isinstance(v, str) and not v.isascii() for v in r.values())),
]

# 真实台账实测到的全部字段名（2026-09-19，277 条记录的并集）。
_KNOWN_KEYS = {
    "created_at", "email", "username", "password", "api_key", "key_id",
    "credits", "sso_uid", "jwt", "status", "error", "stages", "timings",
    "proxy_slot", "verify", "login_ms", "sso_username", "sso_email",
    "grant_has_received", "grant_claimed_now", "balance_raw", "key_count",
    "key_names", "key_created", "key_masked", "key_status",
    "verify_key_source", "downstream", "downstream_ms", "timings_downstream",
    "downstream_at", "verify_models", "verify_reply", "verify_usage", "source",
}


@pytest.mark.parametrize(("label", "pred"), REQUIRED_SHAPES,
                         ids=[s[0].split("（")[0] for s in REQUIRED_SHAPES])
def test_sample_covers_the_shape(sample, label, pred):
    """每一档形状都至少要有**一条**记录。

    失败信息会直接点名缺的是哪一档 —— 样本瘦身时不用猜。
    """
    hits = [r.get("email") or "<email 为空>" for r in sample if pred(r)]
    assert hits, (
        f"样本缺少这一档形状：{label}\n"
        f"（当前样本 {len(sample)} 条，覆盖的键集合："
        f"{sorted({k for r in sample for k in r})}）"
    )


def test_sample_field_names_are_all_known(sample):
    """样本**可以**含未知字段（那是刻意的前向兼容档），但已知字段名不许写错。

    这条挡的是手写 JSON 时的拼写错误 —— 比如把 `sso_uid` 写成 `sso_id`，
    那会让样本"看起来有这一档"而实际测的是另一个键。
    """
    unknown = sorted({k for r in sample for k in r} - _KNOWN_KEYS)
    assert unknown == ["field_not_in_this_version"], (
        f"出现了预期之外的未知字段：{unknown}\n"
        "（唯一刻意保留的未知字段是 `field_not_in_this_version`；"
        "其余未知键名多半是拼写错误 —— 请对照真实台账的字段并集）"
    )


# ═══════════════════════════════════════════════════════════════════
# 三、样本必须是**编造**的 —— 不许有人"顺手"把真数据粘进来
# ═══════════════════════════════════════════════════════════════════
# 与 `tools/gates/check_leaks.py` 的 TOKEN_RE 保持同一套模式。
# 这里再查一遍的意义：**在测试里红**比**在提交闸门里红**更早、更靠近改动者。
_TOKEN_RE = re.compile(
    r"\b(cfat_[A-Za-z0-9]{16,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|ghu_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r")\b")
# 真 JWT 是 `eyJ...` 三段 base64，每段都很长；样本里只有短占位。
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9._\-]{20,}")
# 高熵串 —— 与闸门第 2 层同一条规则（`.json` 属于数据类文件，会被它扫）。
_HIGH_ENTROPY_RE = re.compile(r"[A-Za-z0-9+/=_\-]{28,}")


def test_sample_contains_no_real_looking_credentials():
    """样本里不许出现像真凭据的东西 —— 它是**公开仓库**里的文件。"""
    text = SAMPLE.read_text(encoding="utf-8")
    assert not _TOKEN_RE.findall(text), f"样本里有 token 形态的串：{_TOKEN_RE.findall(text)}"
    assert not _JWT_RE.findall(text), f"样本里有 JWT 形态的串：{_JWT_RE.findall(text)}"
    assert not _HIGH_ENTROPY_RE.findall(text), (
        f"样本里有高熵串（提交闸门会拦）：{_HIGH_ENTROPY_RE.findall(text)}"
    )


def test_sample_emails_use_the_reserved_example_domain(sample):
    """所有 email 必须落在 RFC 2606 保留域 —— 一眼就能看出是编造的。"""
    domains = sorted({e.split("@")[-1] for e in
                      (r["email"] for r in sample if r.get("email"))})
    assert domains == ["example.com"], (
        f"样本 email 的域名是 {domains}；只允许 `example.com`（RFC 2606 保留域）"
    )
