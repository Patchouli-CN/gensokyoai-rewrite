from collections.abc import Callable
from dataclasses import dataclass
from string import Template


@dataclass
class Prompt:
    """提示词。

    - data 式（默认）：`template` 为含 `$var` 占位符的模板字符串，
      渲染用 `string.Template.safe_substitute(**params)`。
    - 原生式：可设 `renderer`（接收 `**params` 的 Python 函数），
      渲染时直接调用它、返回拼好的字符串——条件/循环/分支都原生写。
    """

    template: str = ""
    """ 数据式模板字符串（含 $var 占位符）；原生式下可为空 """
    name: str = "默认文本"
    renderer: Callable | None = None
    """ 原生式渲染函数（接收 **params）；有则优先于 template """

    def render(self, **params) -> str:
        """渲染文本：有 renderer 走原生 Python，否则走 string.Template。"""
        if self.renderer is not None:
            return self.renderer(**params)
        return Template(self.template).safe_substitute(**params)

    def raw(self) -> str:
        """未渲染的模板（原生式返回空字符串）"""
        return self.template
