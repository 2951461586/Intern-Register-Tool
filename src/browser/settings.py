"""浏览器层的**显式注入面** —— `src/browser/` 需要的全部外部配置。

为什么需要它
------------
`src/browser/` 是 `src/` 的子包。改造前它直接 `from .. import config` 去读
`CHROME_PATH` / `SSO_BASE` / `DISCOVERY_BASE` / `CLIENT_ID` / `SOURCE`
（共 3 处：`session.py` / `urls.py` / `waf.py`）。那是一条**指向父包的回边**，
于是 `src <-> src/browser` 在目录粒度上成环。

回边本身不致命（`config` 是叶子，环不可传递），但它绑死两件事：

  · browser **没法脱离 `src/` 单独测试** —— 必须先 `import common.config` 把
    `.env` 灌进 `os.environ`，否则行为静默不同（`constants.py` 读 env）；
  · `config` 一旦不再是叶子，环立刻变成可传递的**真环**。

⇒ 改成**从边界注入**：`src/` 侧构造一个 `BrowserSettings` 传进来，browser 侧
只认它。本模块定义在 browser 包内 ⇒ 对 `src/` 零依赖。

🔴 刻意**不 import 任何配置模块**（`from_config()` 走鸭子类型）。
  加一个 import（哪怕只是 `TYPE_CHECKING`）就等于把回边加回来，
  `tests/test_dependency_surface.py::test_browser_src_boundary_is_respected`
  会当场红。

🔴 阶段 B 把 `config` 搬到了顶层 `common/`（不再在 `src/` 里）—— 那一步的
  目的不是消环（阶段 A 已经消了），而是让"回边长回来"变成**编译期**错误：
  `src/browser/xxx.py` 里的 `from .. import config` 现在会直接 ImportError，
  而不是静默地把环拉回来。**但也不许改成 `from common import config`** ——
  那同样是绕过注入面。本包对项目内所有其他包都应该是零依赖。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class BrowserSettings:
    """`src/browser/` 需要的全部外部配置。字段与 `common/config.py` 一一对应。

    ⚠ `frozen=True` 是**契约**：browser 内的模块只读不写。以前它们读的是
      `config` 模块的全局，谁都能改（探针就干过 `bl.CHROME_ARGS = ...` 这种
      事，见 `_launch_kwargs()` 的 docstring）。冻结之后注入面是显式的，
      改不了就退化成"必须传参"，这正是我们要的。
    """

    chrome_path: str
    sso_base: str
    discovery_base: str
    client_id: str
    source: str

    @classmethod
    def from_config(cls, cfg) -> "BrowserSettings":
        """从**鸭子类型**的配置源构造（本项目传 `common.config` 模块本身）。

        ⚠ 只按属性名取值 —— 不 import、不做 `isinstance` 检查。
          所以任何同形对象都能用（测试里就是拿一个 `SimpleNamespace` 注的），
          而这也正是本模块能不依赖 `src/` 的原因。
        """
        return cls(
            chrome_path=cfg.CHROME_PATH,
            sso_base=cfg.SSO_BASE,
            discovery_base=cfg.DISCOVERY_BASE,
            client_id=cfg.CLIENT_ID,
            source=cfg.SOURCE,
        )
