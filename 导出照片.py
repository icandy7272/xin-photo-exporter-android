#!/usr/bin/env python3
"""双击或 `python3 导出照片.py` 启动导出向导。

放在仓库根目录、用中文命名，是因为使用者是家长和老师：他们要敲的那一行
越短越像人话越好。真正的逻辑都在 tools/ 里。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools import wizard  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(wizard.main())
