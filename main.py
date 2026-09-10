"""仓库内的开发入口 —— 等价于安装后的 `gensokyoai` 命令。

装配逻辑已收进 `gensokyoai.app`（否则 `pip install` 后没有入口），
这里只保留一个薄壳，方便在仓库里 `python main.py` 直接跑。
"""

from gensokyoai.app import main

if __name__ == "__main__":
    raise SystemExit(main())
