"""Windows 平台代理层：注册表读写、残留代理自愈、系统代理探测（2026-09）。

这些用例**全部在 macOS 上跑**：``win.py`` 把 ``winreg`` / ``ctypes`` 都做成函数内
惰性 import，正是为了这一点（``win.py`` 的模块 docstring 写了）。开发机是 mac，
Windows 二进制只能靠 CI 与真机验证，所以这一层必须用桩把逻辑钉死。

为什么这组测试值得写这么细：Windows 的系统代理是 HKCU 里的**全局开关**，
写错了或者残留了，**整机上不了网**（不像 macOS 是逐网络服务、影响面小）。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.infra.platform import base as base_mod  # noqa: E402
from mp_harvest.infra.platform import win as win_mod  # noqa: E402
from mp_harvest.infra.platform.win import (  # noqa: E402
    WinProxyManager,
    parse_proxy_server,
    read_system_proxy,
)

REG_KEY = win_mod._REG_KEY


class _FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install_fake_winreg(monkeypatch, values: dict | None = None):
    """把 ``winreg`` 塞进 sys.modules（惰性 import 会命中它），返回 (store, log)。

    ``log`` 记录所有 SetValueEx 调用，断言「写了哪几个键、写成什么」用。
    """
    reg = types.ModuleType("winreg")
    reg.HKEY_CURRENT_USER = 0x80000001
    reg.KEY_SET_VALUE = 0x0002
    reg.KEY_QUERY_VALUE = 0x0001
    reg.REG_DWORD = 4
    reg.REG_SZ = 1
    store: dict = dict(values or {})
    log: list[tuple[str, object]] = []

    def OpenKey(root, path, reserved=0, access=0):
        if path != REG_KEY:
            raise FileNotFoundError(path)
        return _FakeKey()

    def QueryValueEx(key, name):
        if name not in store:
            raise FileNotFoundError(name)
        return (store[name], reg.REG_SZ)

    def SetValueEx(key, name, reserved, type_, value):
        log.append((name, value))
        store[name] = value

    reg.OpenKey = OpenKey
    reg.QueryValueEx = QueryValueEx
    reg.SetValueEx = SetValueEx
    monkeypatch.setitem(sys.modules, "winreg", reg)
    return store, log


@pytest.fixture()
def no_notify(monkeypatch):
    """``_notify()`` 走 ``ctypes.windll``，mac 上没有 —— 单独测它，其余用例跳过。"""
    monkeypatch.setattr(WinProxyManager, "_notify", staticmethod(lambda: None))


# ── parse_proxy_server：注册表值的两种写法 ────────────────────────


def test_parse_plain_host_port():
    assert parse_proxy_server("127.0.0.1:7897") == "127.0.0.1:7897"
    assert parse_proxy_server(" 127.0.0.1:7897 ") == "127.0.0.1:7897"


def test_parse_scheme_list_prefers_https():
    """按协议分列时取 https —— 检查更新与下载走的都是 https。"""
    assert parse_proxy_server("http=1.2.3.4:1;https=5.6.7.8:2") == "5.6.7.8:2"
    assert parse_proxy_server("http=1.2.3.4:1") == "1.2.3.4:1"


def test_parse_empty_is_empty():
    for raw in ("", "   ", None, "  ;  "):
        assert parse_proxy_server(raw) == ""  # type: ignore[arg-type]


# ── read_system_proxy：探测 + 自环过滤 ────────────────────────────


def test_read_system_proxy_returns_url(monkeypatch):
    install_fake_winreg(monkeypatch, {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7897"})
    assert read_system_proxy() == "http://127.0.0.1:7897"


def test_read_system_proxy_disabled_is_empty(monkeypatch):
    install_fake_winreg(monkeypatch, {"ProxyEnable": 0, "ProxyServer": "127.0.0.1:7897"})
    assert read_system_proxy() == ""


def test_read_system_proxy_skips_own_capture_proxy(monkeypatch):
    """抓包开着时系统代理本来就指向我们自己 —— 那不是可用的出口。

    否则「跟随系统代理」会让检查更新/下载绕回本机 8088，能不能通完全取决于抓包
    代理还活着没有。见 ``read_system_proxy`` 的 docstring。
    """
    install_fake_winreg(monkeypatch, {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:8088"})
    assert read_system_proxy() == ""


def test_read_system_proxy_survives_broken_registry(monkeypatch):
    """注册表读不到时返回空串，不抛。"""
    reg = types.ModuleType("winreg")
    reg.HKEY_CURRENT_USER = 1
    reg.KEY_QUERY_VALUE = 1

    def OpenKey(*a, **k):
        raise OSError("拒绝访问")

    reg.OpenKey = OpenKey
    monkeypatch.setitem(sys.modules, "winreg", reg)
    assert read_system_proxy() == ""


def test_read_system_proxy_without_winreg_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "winreg", None)
    assert read_system_proxy() == ""


# ── _system_proxy 的 win32 分支 + 自环守卫（base.py，两平台共用）──


def test_system_proxy_win32_branch(monkeypatch):
    install_fake_winreg(monkeypatch, {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7897"})
    monkeypatch.setattr(base_mod.sys, "platform", "win32")
    assert base_mod._system_proxy() == "http://127.0.0.1:7897"


def test_system_proxy_filters_own_proxy_on_all_platforms(monkeypatch):
    """自环守卫：mac 走 scutil、win 走注册表，两条路都要过滤掉自己。"""
    install_fake_winreg(monkeypatch, {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:8088"})
    monkeypatch.setattr(base_mod.sys, "platform", "win32")
    assert base_mod._system_proxy() == ""

    monkeypatch.setattr(base_mod.sys, "platform", "darwin")
    monkeypatch.setattr(
        base_mod, "_detect_system_proxy", lambda: "http://127.0.0.1:8088"
    )
    assert base_mod._system_proxy() == ""


def test_is_own_capture_proxy_variants():
    f = base_mod._is_own_capture_proxy
    assert f("http://127.0.0.1:8088")
    assert f("127.0.0.1:8088")
    assert f("http://127.0.0.1:8088/")
    assert not f("http://127.0.0.1:7897")
    assert not f("http://192.168.1.1:8088")
    assert not f("")


# ── enable / disable：备份与恢复 ─────────────────────────────────


def test_enable_writes_registry_and_backs_up(monkeypatch, no_notify):
    store, log = install_fake_winreg(
        monkeypatch, {"ProxyEnable": 1, "ProxyServer": "10.0.0.1:1080", "ProxyOverride": "corp"}
    )
    mgr = WinProxyManager()
    res = mgr.enable(8088)
    assert res.ok, res.message
    assert store["ProxyEnable"] == 1
    assert store["ProxyServer"] == "127.0.0.1:8088"
    assert store["ProxyOverride"] == "<local>"
    # 备份的是**原标题**，不是写完之后的值
    assert mgr._backup == {
        "ProxyEnable": "1",
        "ProxyServer": "10.0.0.1:1080",
        "ProxyOverride": "corp",
    }
    assert ("ProxyServer", "127.0.0.1:8088") in log


def test_enable_twice_does_not_clobber_backup(monkeypatch, no_notify):
    """重复 enable 不能覆盖备份，否则 disable 会「恢复」成我们自己的地址。"""
    install_fake_winreg(monkeypatch, {"ProxyEnable": 1, "ProxyServer": "10.0.0.1:1080"})
    mgr = WinProxyManager()
    mgr.enable(8088)
    mgr.enable(8088)
    assert mgr._backup["ProxyServer"] == "10.0.0.1:1080"


def test_disable_restores_backup(monkeypatch, no_notify):
    store, _ = install_fake_winreg(
        monkeypatch, {"ProxyEnable": 1, "ProxyServer": "10.0.0.1:1080", "ProxyOverride": "corp"}
    )
    mgr = WinProxyManager()
    mgr.enable(8088)
    res = mgr.disable()
    assert res.ok, res.message
    assert store["ProxyServer"] == "10.0.0.1:1080"
    assert store["ProxyOverride"] == "corp"
    assert store["ProxyEnable"] == 1
    assert mgr._backup is None


def test_disable_without_backup_is_noop(monkeypatch, no_notify):
    """不是本应用开的代理就不能动 —— 返回 ok 但一个键都不写。"""
    _, log = install_fake_winreg(monkeypatch, {"ProxyEnable": 1, "ProxyServer": "10.0.0.1:1080"})
    res = WinProxyManager().disable()
    assert res.ok
    assert log == []


def test_enable_reports_failure_when_registry_raises(monkeypatch, no_notify):
    reg = types.ModuleType("winreg")
    reg.HKEY_CURRENT_USER = 1
    reg.KEY_QUERY_VALUE = 1
    reg.KEY_SET_VALUE = 2
    reg.REG_DWORD = 4
    reg.REG_SZ = 1
    reg.OpenKey = lambda *a, **k: (_ for _ in ()).throw(OSError("拒绝访问"))
    reg.QueryValueEx = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
    reg.SetValueEx = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "winreg", reg)
    res = WinProxyManager().enable(8088)
    assert not res.ok
    assert "拒绝访问" in (res.error or "")


# ── recover_stale：崩溃残留自愈（本轮新增的正题）────────────────


def _dead_port(monkeypatch):
    monkeypatch.setattr("socket.create_connection", _raise_oserror)


def _raise_oserror(*a, **k):
    raise OSError("connection refused")


def test_recover_stale_disables_our_leftover(monkeypatch, no_notify):
    """端口已死 + 注册表指向我们 → 必须关掉，否则整机断网。"""
    store, log = install_fake_winreg(
        monkeypatch, {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:8088"}
    )
    _dead_port(monkeypatch)
    res = WinProxyManager().recover_stale()
    assert res.ok
    assert "已恢复" in res.message
    assert store["ProxyEnable"] == 0
    assert ("ProxyEnable", 0) in log


def test_recover_stale_leaves_user_proxy_alone(monkeypatch, no_notify):
    """指向别处（用户自己的 Clash）时**一个键都不能动**，哪怕端口不通。"""
    store, log = install_fake_winreg(
        monkeypatch, {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7897"}
    )
    _dead_port(monkeypatch)
    res = WinProxyManager().recover_stale()
    assert res.ok
    assert store["ProxyServer"] == "127.0.0.1:7897"
    assert store["ProxyEnable"] == 1
    assert log == []
    assert "未发现" in res.message


def test_recover_stale_noop_when_port_listening(monkeypatch, no_notify):
    """端口活着说明抓包正常在跑 —— 那是我们要的状态，不能关。"""
    store, log = install_fake_winreg(
        monkeypatch, {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:8088"}
    )
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: _FakeKey())
    res = WinProxyManager().recover_stale()
    assert res.ok
    assert store["ProxyEnable"] == 1
    assert log == []


def test_recover_stale_noop_when_proxy_disabled(monkeypatch, no_notify):
    store, log = install_fake_winreg(
        monkeypatch, {"ProxyEnable": 0, "ProxyServer": "127.0.0.1:8088"}
    )
    _dead_port(monkeypatch)
    res = WinProxyManager().recover_stale()
    assert res.ok
    assert log == []


def test_recover_stale_reports_failure(monkeypatch, no_notify):
    """注册表写不进去时要**报错**，不能装作恢复了（否则用户查不出为什么还断网）。"""
    reg = types.ModuleType("winreg")
    reg.HKEY_CURRENT_USER = 1
    reg.KEY_QUERY_VALUE = 1
    reg.KEY_SET_VALUE = 2
    reg.REG_DWORD = 4
    reg.REG_SZ = 1
    reg.OpenKey = lambda *a, **k: _FakeKey()
    reg.QueryValueEx = lambda key, name: (1 if name == "ProxyEnable" else "127.0.0.1:8088", 4)

    def boom(*a, **k):
        raise OSError("写注册表失败")

    reg.SetValueEx = boom
    monkeypatch.setitem(sys.modules, "winreg", reg)
    _dead_port(monkeypatch)
    res = WinProxyManager().recover_stale()
    assert not res.ok
    assert "写注册表失败" in (res.error or "")


# ── _notify：让运行中的程序立刻感知（单独测，其余用例都把它桩掉）──


def test_notify_calls_internet_set_option(monkeypatch):
    calls: list[tuple] = []

    class _Wininet:
        @staticmethod
        def InternetSetOptionW(*args):
            calls.append(args)
            return 1

    fake_ctypes = types.ModuleType("ctypes")
    fake_ctypes.windll = types.SimpleNamespace(wininet=_Wininet)
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)

    WinProxyManager._notify()
    # 39 = INTERNET_OPTION_SETTINGS_CHANGED，37 = INTERNET_OPTION_REFRESH
    assert [c[1] for c in calls] == [39, 37]
