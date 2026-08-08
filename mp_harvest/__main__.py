"""``python -m mp_harvest`` / PyInstaller 入口。"""

from __future__ import annotations

import sys

from mp_harvest.shell.main import main

if __name__ == "__main__":
    raise SystemExit(main())
