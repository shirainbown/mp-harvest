"""MP Harvest FastAPI 服务层（设计稿 §3.1/§7）。

包级单例：启动一次性 token —— 所有 ``/api/*`` 与 ``/ws`` 均需携带
（query ``?token=`` 或 ``Authorization: Bearer``），防本机恶意进程调用（§3.5）。
"""

from __future__ import annotations

import secrets

_token: str | None = None


def get_token() -> str:
    """进程级一次性 token（首次调用时生成，之后不变）。"""
    global _token
    if _token is None:
        _token = secrets.token_urlsafe(32)
    return _token


def reset_token() -> None:
    """重置 token（测试用）。"""
    global _token
    _token = None
