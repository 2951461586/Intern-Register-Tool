"""把仓库根加进 `sys.path` —— 让 `python tools/xxx.py` 能 `from src import ...`。

为什么需要它
------------
`tools/` 里的脚本是用 `python tools/xxx.py` 跑的，此时 `sys.path[0]` 是
`tools/` 而不是仓库根，于是 `import src` 会失败。

以前的做法是**每个文件各写一遍** `sys.path.insert(...)` —— 22 处、两种写法
（`str(Path(__file__).resolve().parents[1])` 和先算 `ROOT` 再 insert），
于是"仓库根怎么算"这件事有 22 份实现。改一处容易漏另一处，
而漏掉的那份会以 `ModuleNotFoundError: No module named 'src'` 收场 ——
**报错指向 import，不指向路径**，排查方向是错的。

现在收敛到这一处。用法（放在所有 `from src ...` 之前）：

    from _bootstrap import ROOT  # noqa: F401  （副作用：把仓库根加进 sys.path）

`ROOT` 也可以直接拿来用（它等于仓库根），所以原来那些
`ROOT = Path(__file__).resolve().parents[1]` 赋值行可以一并删掉。

⚠ 为什么是 `from _bootstrap import ...` 而不是 `from tools._bootstrap import ...`：
  `tools/` **刻意不是包**（没有 `__init__.py`），这样 `python tools/xxx.py`
  跑起来 `tools/` 就在 `sys.path[0]`，同目录 import 天然可用。
  加 `__init__.py` 会把 28 个脚本变成包成员，反而要改所有调用方式。

⚠ 为什么不用 `pip install -e .` 替代：那要求使用者先跑一次安装，
  而本项目刻意"clone 下来就能跑"。`pyproject.toml` 里保留了安装配置，
  装了也不会冲突（这里的 `insert` 会先命中）。

它还顺手加载 `.env`（见文件末尾）—— 理由见下。
"""

import sys
from pathlib import Path

# tools/_bootstrap.py -> parents[0] = tools/，parents[1] = 仓库根
# ⚠ 这一行是**本文件存在的唯一理由**，任何批量脚本都不许删它（见下方"为什么这里
#   没有用 `from _bootstrap import ROOT`"）。删掉它会让 22 个工具的 import 全断，
#   且报错是 `NameError: name 'ROOT' is not defined`，指向 import 而非路径。
ROOT = Path(__file__).resolve().parents[1]

# 幂等：已在路径里就不重复插（重复插会让 sys.path 随 import 次数增长）
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── 加载 `.env` ─────────────────────────────────────────────────────
# 必须排在 `sys.path` 插入**之后**，且必须早于任何 `os.getenv("IR_*")` 求值。
#
# 复用 `src/config.py` 的实现（它在导入时执行 `_load_dotenv(仓库根/.env)`），
# 而不是在这里再写一份解析 —— `.env` 解析有若干细节（只填未设置的键、
# 剥引号、忽略注释），两份实现必然漂移成"一个生效一个不生效"。
#
# 🔴 为什么收敛到这一处：`tools/` 下有 20+ 个脚本直接读 `IR_*`，
#    逐个要求"记得 import config"是**必然会被漏**的方案。实测已漏两个：
#
#      * `tools/ops/proxypool_ctl.py` —— 只 import 了 `_path`，于是
#        `Path(os.getenv("IR_MIHOMO_EXE", ""))` 拿到 `Path("")` == `Path(".")`，
#        报错文案是 `✗ 找不到内核：.` —— **完全看不出真因是"没加载 .env"**，
#        会把人引去检查"路径是不是写错了"（而路径根本没被读到）。
#      * `tools/ops/cf_service_doctor.py` —— 同理，报"缺少 IR_WORKER_BASE"，
#        看起来像没配，实际是没读。
#
#    这类失败的特征是**报错指向症状、不指向原因**，所以不能靠"下次注意"，
#    要靠结构上不可能漏：所有 `tools/` 脚本都经过 `_path` → 本文件。
#
# ⚠ `src/` 内部的模块**不受**这里保护（它们不经过本文件）。已知隐患：
#    `src/browser/constants.py` 在导入时用 `os.getenv` 定值，若它先于
#    `src.config` 被导入，`.env` 里配的 `IR_MICRO_BUDGET` 等会静默失效。
#    当前 `.env` 没配这些键，所以无实际影响；改 `.env` 时留意。
try:
    import src.config  # noqa: F401
except ImportError:
    # 没有 src 包的环境（例如只想用本文件算 ROOT）不应因此崩掉。
    pass

