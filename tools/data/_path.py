"""`tools/<子目录>/` 专用垫片：把 `tools/` 加进 `sys.path`，再转出 `tools/_bootstrap.ROOT`。

为什么需要它
------------
`sys.path[0]` 是**脚本所在目录**。脚本移进子目录后 `tools/` 不再在路径里，
`from _bootstrap import ROOT` 会直接 ImportError（报错指向 import，不指向路径，
排查方向是错的）。本文件把那两行补上，"仓库根怎么算"仍然只有
`tools/_bootstrap.py` 一处实现。

⚠ 本文件**不能**叫 `_bootstrap.py`：那样它会 import 到自己 —— `sys.modules`
里已有同名（半成品）模块，会报
`cannot import name 'ROOT' from partially initialized module`。名字不同是刻意的。

用法（放在所有 `from src ...` 之前）：

    from _path import ROOT  # noqa: F401  （副作用：把 tools/ 与仓库根加进 sys.path）
"""

import sys
from pathlib import Path

# tools/<子目录>/_path.py -> parents[0] = 子目录，parents[1] = tools/
_TOOLS = Path(__file__).resolve().parents[1]
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from _bootstrap import ROOT  # noqa: E402,F401
