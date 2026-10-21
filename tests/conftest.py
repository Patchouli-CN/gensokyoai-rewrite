"""测试公共夹具 —— 场景注册表是全局的，每个用例跑完复原。"""

import pytest

from gensokyoai import api


@pytest.fixture(autouse=True)
def _isolate_scenes():
    saved = dict(api._SCENES)
    yield
    api._SCENES.clear()
    api._SCENES.update(saved)
