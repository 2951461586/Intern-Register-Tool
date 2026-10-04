"""跨层共享的**叶子**基础设施。

为什么单独成包（2026-10-04，阶段 B）
-----------------------------------
`config` 原先住在 `src/config.py`，即**在被使用者内部**。`src/` 里的每个
模块都 `from . import config`，而 `src/browser/`（`src` 的子包）也要用它 ——
于是它在目录粒度上把 `src` 和 `src/browser` 拉成了环。

阶段 A 先用**参数注入**把那条回边去掉了（`src/browser/settings.py`：
browser 改收 `BrowserSettings`，不再 `from .. import config`）。那之后环已经
不存在了 —— 也就是说本包**不是为了消环**才建的。

真正要解决的是"回边会**悄无声息地长回来**"：

    src/browser/xxx.py 里写 `from .. import config`
    → 在阶段 A 之前：能跑、不报错，环回来了，没有任何红灯
    → 在 `config` 搬出 `src/` 之后：ImportError，写的人立刻知道走错了路

这与 `tools/_bootstrap.py` 是同一个思路 —— "不能靠下次注意，要靠结构上
不可能漏"。把 `config` 放到**使用者们的同级**（而不是某个使用者的内部），
`..`/`.` 就不再能指到它。

约定
----
· 本包只放**叶子**：只依赖 stdlib，不 import `src/` 或本包以外的项目代码。
  `tests/test_dependency_surface.py::test_config_module_stays_a_leaf` 钉住了它。
· 放东西进来之前先问："它是不是被 `src/` 的多条不相干分支共用？"
  只有 `config` 符合；`fsutil` / `redact` 目前只在 `src/` 内部流转，留在原处。
"""
