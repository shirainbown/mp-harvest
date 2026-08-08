#!/usr/bin/env python3
"""Zero-dependency test runner for the plain `def test_*` test modules.

Usage:
    python mp_harvest/tests/run_tests.py            # run every mp_harvest/tests/test_*.py
    python mp_harvest/tests/run_tests.py test_batch_import test_ai_filter

Works with the system Python (no pytest needed). Modules that fail to import
(missing third-party deps) are reported and skipped so remaining tests still run.
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parents[1]  # mp_harvest/tests -> 仓库根（包 `mp_harvest` 可导入）
sys.path.insert(0, str(REPO_ROOT))


def _load_module(name: str):
    return importlib.import_module(f"mp_harvest.tests.{name}")


def _test_functions(module) -> list:
    return [
        fn
        for fn in vars(module).values()
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_")
        and getattr(fn, "__module__", "") == module.__name__
    ]


def main() -> int:
    names = sys.argv[1:] or sorted(
        p.stem for p in TESTS_DIR.glob("test_*.py")
    )
    failed: list[tuple[str, str, str]] = []
    skipped: list[str] = []
    total = passed = 0

    for name in names:
        try:
            module = _load_module(name)
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{name}（导入失败: {exc}）")
            continue
        for fn in _test_functions(module):
            total += 1
            try:
                fn()
                passed += 1
            except Exception:
                failed.append((name, fn.__name__, traceback.format_exc()))

    for name in skipped:
        print(f"SKIP  {name}")
    for name, fn, tb in failed:
        print(f"FAIL  {name}.{fn}\n{tb}")
    print(f"\n{passed}/{total} passed" + (f" · {len(skipped)} skipped" if skipped else ""))
    if failed:
        print(f"{len(failed)} FAILED")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
