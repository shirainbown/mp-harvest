"""平台抽象入口：``from mp_harvest.infra.platform import get_platform``。"""

from mp_harvest.infra.platform import paths
from mp_harvest.infra.platform.base import (
    APP_VERSION,
    DownloadResult,
    InstallResult,
    Platform,
    PlatformError,
    ProxyResult,
    UpdateCheckResult,
    get_platform,
    reset_platform,
)

__all__ = [
    "APP_VERSION",
    "DownloadResult",
    "InstallResult",
    "Platform",
    "PlatformError",
    "ProxyResult",
    "UpdateCheckResult",
    "get_platform",
    "paths",
    "reset_platform",
]
