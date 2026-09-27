"""仓库内的开发入口 —— 等价于安装后的 `gensokyoai` 命令。

薄壳：装配逻辑在 `gensokyoai.app`，仓库里 `python main.py` 直接跑。
"""

from gensokyoai.app import main

if __name__ == "__main__":
    raise SystemExit(main())
