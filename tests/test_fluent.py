"""fluent 链式基类测试：追加 / 合并 / 不可变 / 只读视图 / Self 类型保持"""

from gensokyoai.utils.fluent import FluentAPI


class _Chain(FluentAPI[int]):
    """最小实现：验证基类契约（_with 造同型实例）"""

    def __init__(self, label: str = "", *items: int) -> None:
        super().__init__(items)
        self.label = label

    def _with(self, items: tuple[int, ...]) -> _Chain:
        return _Chain(self.label, *items)


def test_append_returns_new_instance_and_preserves_type():
    """>> 返回新实例；原链不变；静态类型保持为子类"""
    base = _Chain("demo", 1)
    longer = base >> 2 >> 3

    assert isinstance(longer, _Chain)
    assert longer.label == "demo", "子类字段必须被 _with 保留"
    assert base.items == (1,), "原链不被修改（不可变）"
    assert longer.items == (1, 2, 3)


def test_connect_merges_chains():
    """链 >> 链：展开合并，可复用公共前缀"""
    prefix = _Chain("p", 1, 2)
    merged = prefix >> _Chain("q", 3)
    assert merged.items == (1, 2, 3)
    assert prefix.items == (1, 2)


def test_chaining_keeps_left_label_and_ignores_right_label():
    """合并时以左链的标签为准，右链只贡献元素"""
    left = _Chain("left", 1)
    right = _Chain("right", 2)
    merged = left >> right
    assert merged.label == "left"
    assert merged.items == (1, 2)


def test_readonly_views():
    """len / iter / items 只读视图"""
    chain = _Chain("v") >> 1 >> 2
    assert len(chain) == 2
    assert list(chain) == [1, 2]
    assert chain.items == (1, 2)
    assert "2" in repr(chain)


def test_empty_chain_is_safe():
    """空链可直接起步"""
    chain = _Chain()
    assert len(chain) == 0 and chain.items == ()
    assert (chain >> 1).items == (1,)


def test_items_tuple_is_immutable():
    """暴露的 items 是元组，调用方改不动链"""
    chain = _Chain("t", 1)
    items = chain.items
    assert isinstance(items, tuple)
