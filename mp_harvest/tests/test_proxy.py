"""MacProxyManager.recover_stale：异常退出残留代理自愈（2026-08-09）。"""

from __future__ import annotations

import sys
import types
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.infra.platform.mac import MacProxyManager  # noqa: E402


def test_recover_stale_turns_off_dead_8088():
    m = MacProxyManager()
    calls: list[list[str]] = []
    with mock.patch("socket.create_connection", side_effect=OSError("refused")), mock.patch.object(
        MacProxyManager, "list_services", return_value=["Wi-Fi", "Ethernet"]
    ), mock.patch.object(
        MacProxyManager,
        "_read_state",
        side_effect=lambda svc, kind: (
            {"enabled": "Yes", "server": "127.0.0.1", "port": "8088"}
            if svc == "Wi-Fi"
            else {"enabled": "No", "server": "", "port": ""}
        ),
    ), mock.patch(
        "mp_harvest.infra.platform.mac._run",
        side_effect=lambda cmd, timeout: calls.append(cmd)
        or types.SimpleNamespace(returncode=0),
    ):
        res = m.recover_stale()
    assert res.ok
    assert "已恢复" in res.message
    assert any("setwebproxystate" in " ".join(c) and "Wi-Fi" in c for c in calls)
    assert any("setsecurewebproxystate" in " ".join(c) and "Wi-Fi" in c for c in calls)
    assert not any("Ethernet" in c for c in calls)  # 未开启代理的服务不动


def test_recover_stale_noop_when_port_up():
    m = MacProxyManager()
    calls: list[list[str]] = []
    with mock.patch(
        "socket.create_connection", return_value=nullcontext()
    ), mock.patch(
        "mp_harvest.infra.platform.mac._run",
        side_effect=lambda cmd, timeout: calls.append(cmd)
        or types.SimpleNamespace(returncode=0),
    ):
        res = m.recover_stale()
    assert res.ok
    assert "无需恢复" in res.message
    assert not calls
