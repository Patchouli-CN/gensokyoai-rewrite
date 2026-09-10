"""pytest 共享夹具 / 环境适配。

背景：某些受限环境（沙箱/CI）不允许写系统 Temp（如 F:\\Temp\\dsh-XXXX），
pytest 默认的 `tmp_path`/`basetemp` 把临时目录建在那里，导致所有 `tmp_path`
用例报 PermissionError。此外，`tempfile.mkdtemp` 以 mode=0o700 建目录，本
环境把它当「只读/拒删」，连删除都被锁。

方案：**绕过 pytest 的临时目录机制**——覆写 `tmp_path` 夹具，统一在项目
根目录的 `temp/pytest` 下（可见、集中、易清理）用 `os.makedirs`（默认宽松
mode，可写）建每个用例的独立子目录，用后即删。普通路径写法（不触发
`\\\\?\\` 扩展路径），沙箱里稳定可写、可删。
"""

import os
import shutil
import uuid
from pathlib import Path

import pytest
from _pytest.config import Config

_ROOT = Path(__file__).resolve().parent
_TMP_ROOT = _ROOT / "temp" / "pytest"
""" 测试临时根目录：项目根/temp/pytest，集中可删 """


@pytest.fixture
def tmp_path():
    """替代 pytest 内置 tmp_path：项目根/temp/pytest 下建临时目录，用后即删。

    每个用例一个独立子目录；目录由 `os.makedirs`（默认 mode）创建，
    不经过 `tempfile.mkdtemp` 的 0o700 模式，确保可写可删。
    """
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TMP_ROOT / f"t-{uuid.uuid4().hex[:12]}"
    os.makedirs(path, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def pytest_unconfigure(config: Config) -> None:
    """会话结束后清理整棵临时目录。"""
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)
