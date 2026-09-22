"""链式 API 基类（fluent）：`chain >> item` 追加元素，`chain >> other` 合并两条链。

设计约束（对应架构文档 §7.3 分层铁律）：

- 本模块是 **L0 纯叶子**：只准用标准库，禁止 import 项目内任何模块
  （`tests/test_no_circular_import.py::test_utils_is_leaf` 会过 AST 检查）；
- 链式操作**同步**：拼装阶段不做任何异步，`async` 由使用方在自己的
  `run()`/`execute()` 里做（如 `core/brain/pipeline.py::ThinkPipeline.run`）；
- **不可变**：每次连接返回同型新实例，不在原链上追加。

子类约定：

- 构造时把元素元组交给 `super().__init__(items)`（自己的字段另行保存）；
- 实现 `_with(items)`：用新元素序列造一个**同型新实例**（保留自己的字段）；
- 需要接字面量糖（如裸字符串）时，重写 `__rshift__` 并把参数**放宽**（LSP），
  收编后转调 `super()` 或自行 `_with`。
"""

from collections.abc import Iterator
from typing import Self


class FluentAPI[T]:
    """不可变链式基类：元素追加 / 同型链合并，返回具体子类类型。"""

    __slots__ = ("_items",)

    def __init__(self, items: tuple[T, ...] = ()) -> None:
        """初始化。

        Args:
            items: 初始元素序列（空链起步，逐项 >> 进来）
        """
        self._items = tuple(items)

    # ---------------------------------------------------------------- 子类钩子

    def _with(self, items: tuple[T, ...]) -> Self:
        """用新元素序列造同型新实例（子类必须实现）。"""
        raise NotImplementedError

    # ---------------------------------------------------------------- 连接

    def connect(self, item: T | FluentAPI[T]) -> Self:
        """连接一个元素或另一条同元素类型的链（返回新实例，不在原链上改）。

        Args:
            item: 元素；若传入同类型链则展开合并（公共前缀复用）

        Returns:
            Self: 新链（静态类型保持为调用方的具体子类）
        """
        if isinstance(item, FluentAPI):
            return self._with((*self._items, *item.items))
        return self._with((*self._items, item))

    def __rshift__(self, item: T | FluentAPI[T]) -> Self:
        """`chain >> item` 语法糖，等价于 `connect`。"""
        return self.connect(item)

    # ---------------------------------------------------------------- 只读视图

    @property
    def items(self) -> tuple[T, ...]:
        """元素元组（副本语义外的只读视图，元组本身不可变）。"""
        return self._items

    def __iter__(self) -> Iterator[T]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({', '.join(map(repr, self._items))})"
