#!/usr/bin/env python3
"""MP Harvest 任意目录直接运行入口（2026-08-09）。

用法（在任意目录执行均可，不依赖 cwd）：
    python run.py                            # 生产模式：自动加载/构建前端并开窗口
    python run.py --no-window                # 仅起服务，浏览器调试
    python run.py --dev http://localhost:5173  # Vite 热更开发
    python run.py --hidden-titlebar          # macOS 无边框窗口

原理：把本文件所在目录（仓库根）加入 sys.path，再调用 mp_harvest.shell.main。
生产模式若缺 frontend/dist 会自动 npm 构建（首次需要 Node.js）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mp_harvest.shell.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
