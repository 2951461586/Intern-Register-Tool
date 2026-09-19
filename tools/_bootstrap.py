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
