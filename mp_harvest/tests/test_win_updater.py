"""WindowsUpdater：更新脚本生成、zip 层级、编码与前置校验（2026-09）。

在 macOS 上跑（``win.py`` 全惰性 import；``os._exit`` 与 ``Popen`` 都桩掉）。

这一组盯的是「点了更新，应用关了，然后再也没起来」那一类**完全静默**的故障：
生成出来的 bat 是在应用退出之后才执行的，中间任何一步错了都没有界面能报出来。
"""

from __future__ import annotations

import os
import sys
import types
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import mp_harvest.infra.platform.paths as paths_mod  # noqa: E402
from mp_harvest.infra.platform.base import PlatformError  # noqa: E402
from mp_harvest.infra.platform.win import (  # noqa: E402
    WinUpdater,
    _bat_encoding,
    _build_apply_script,
    _payload_root,
)


@pytest.fixture()
def frozen(monkeypatch):
    """假装处于冻结状态（apply 在开发模式会直接拒绝）。"""
    monkeypatch.setattr(paths_mod, "is_frozen", lambda: True)


@pytest.fixture()
def no_exit(monkeypatch):
    """拦下 ``os._exit(0)`` —— 否则测试进程自己就没了。"""
    monkeypatch.setattr(os, "_exit", lambda code=0: None)


@pytest.fixture()
def no_popen(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "mp_harvest.infra.platform.win.subprocess.Popen",
        lambda cmd, **kw: calls.append(cmd),
    )
    return calls


def _make_install(tmp_path: Path) -> Path:
    """造一份长得像 onedir 安装的目录：``<dir>/MP Harvest.exe`` + ``_internal/``。"""
    install = tmp_path / "MP Harvest"
    install.mkdir(parents=True, exist_ok=True)
    (install / "MP Harvest.exe").write_bytes(b"old")
    (install / "_internal").mkdir(exist_ok=True)
    return install


def _make_zip(tmp_path: Path, *, nested: bool) -> Path:
    """造更新包。``nested`` 决定顶层有没有 ``MP Harvest/`` 这一层。"""
    pkg = tmp_path / "MP-Harvest-win-9.9.9.zip"
    prefix = "MP Harvest/" if nested else ""
    with zipfile.ZipFile(pkg, "w") as zf:
        zf.writestr(f"{prefix}MP Harvest.exe", b"new")
        zf.writestr(f"{prefix}_internal/marker.txt", "x")
    return pkg


# ── asset 命名（atom 兜底拼地址用）────────────────────────────────


def test_asset_prefix_matches_ci_artifact_name():
    """CI 产物是 MP-Harvest-win-<ver>.zip；拼错了 atom 兜底就下不到东西。"""
    assert WinUpdater.asset_prefix == "MP-Harvest-win-"
    assert WinUpdater.asset_suffix == ".zip"


# ── 载荷根：两种 zip 布局都要能用 ────────────────────────────────


def test_payload_root_descends_into_single_top_dir(tmp_path):
    d = tmp_path / "extracted"
    (d / "MP Harvest" / "_internal").mkdir(parents=True)
    assert _payload_root(d) == d / "MP Harvest"


def test_payload_root_keeps_flat_layout(tmp_path):
    """平铺的 zip（exe 与 _internal 直接躺在根上）不能被误判成「有个子目录」。"""
    d = tmp_path / "extracted"
    (d / "_internal").mkdir(parents=True)
    (d / "MP Harvest.exe").write_bytes(b"")
    assert _payload_root(d) == d


def test_payload_root_ignores_macos_junk(tmp_path):
    """zip 里混进 __MACOSX/ 时不能把它当成「唯一的子目录」。"""
    d = tmp_path / "extracted"
    (d / "__MACOSX").mkdir(parents=True)
    (d / "MP Harvest.exe").write_bytes(b"")
    assert _payload_root(d) == d


# ── bat 内容 ─────────────────────────────────────────────────────


def test_apply_script_waits_polls_and_reports(tmp_path):
    install_dir = tmp_path / "MP Harvest"
    s = _build_apply_script(
        payload=tmp_path / "payload",
        install_dir=install_dir,
        exe_name="MP Harvest.exe",
        pid=4242,
        log_path=tmp_path / "update.log",
    )
    # 等应用真的退出（而不是 timeout —— 见下面那条用例）
    assert 'tasklist /FI "PID eq 4242" /NH' in s
    assert ":waitloop" in s and ":ready" in s
    # 覆盖
    assert 'xcopy /E /Y /I' in s
    # 覆盖失败**不能**再启动应用（会起出一个半旧半新的安装）
    assert ":failed" in s
    failed_block = s.split(":failed", 1)[1].split(":done", 1)[0]
    assert "start " not in failed_block
    # 成功才重启，且重启的是安装目录里的 exe
    assert f'start "" "{install_dir / "MP Harvest.exe"}"' in s
    # 自删
    assert 'del "%~f0"' in s


def test_apply_script_never_uses_timeout(tmp_path):
    """``timeout`` 在没有控制台时直接报错退出（GUI 程序拉起的 cmd 就是这种）。

    原先用它做延时，被 ``>nul`` 藏住了报错 —— 于是**根本没等**，xcopy 去覆盖一个
    还开着的 exe，报「拒绝访问」，更新静默失败。
    """
    s = _build_apply_script(
        payload=tmp_path / "p",
        install_dir=tmp_path,
        exe_name="MP Harvest.exe",
        pid=1,
        log_path=tmp_path / "l.log",
    )
    assert "timeout " not in s
    assert "ping -n 2 127.0.0.1 >nul" in s


def test_apply_script_crlf_is_not_doubled(tmp_path):
    """脚本必须**原样**是 \\r\\n。

    ``Path.write_text`` 的 newline 默认 None，在 Windows 上会把文本里的 "\\n"
    再翻一遍 → "\\r\\r\\n"。这里对文本本身断言（写入见下一条用例）。
    """
    s = _build_apply_script(
        payload=tmp_path / "p",
        install_dir=tmp_path,
        exe_name="e.exe",
        pid=1,
        log_path=tmp_path / "l.log",
    )
    assert "\r\r\n" not in s
    assert s.count("\n") == s.count("\r\n")


# ── 编码 ─────────────────────────────────────────────────────────


def test_bat_encoding_uses_oem_codepage(monkeypatch):
    """必须按 OEM 代码页写 —— cmd.exe 就是按它解析 .bat 的。

    中文 Windows 上安装路径几乎必然含中文（用户名），用 UTF-8 写就是乱码，
    xcopy 找不到路径。
    """
    fake_ctypes = types.ModuleType("ctypes")
    fake_ctypes.windll = types.SimpleNamespace(
        kernel32=types.SimpleNamespace(GetOEMCP=lambda: 936)
    )
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    assert _bat_encoding() == "cp936"


def test_bat_encoding_falls_back_when_no_oemcp(monkeypatch):
    monkeypatch.setitem(sys.modules, "ctypes", None)
    assert _bat_encoding()  # 非空即可：非 Windows 上不会真去执行这个文件


# ── apply()：前置校验与整条链路 ──────────────────────────────────


def test_apply_dev_mode_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(paths_mod, "is_frozen", lambda: False)
    pkg = tmp_path / "MP-Harvest-win-9.9.9.zip"
    pkg.write_bytes(b"")
    with pytest.raises(PlatformError) as ei:
        WinUpdater().apply(pkg)
    assert "开发模式" in str(ei.value)


def test_apply_missing_package_raises(frozen, tmp_path):
    with pytest.raises(PlatformError) as ei:
        WinUpdater().apply(tmp_path / "nope.zip")
    assert "不存在" in str(ei.value)


def test_apply_rejects_unwritable_install_dir(frozen, monkeypatch, tmp_path, no_exit, no_popen):
    """安装目录不可写时要在**退出进程之前**报错。

    便携版被解压到 Program Files 或同步盘里很常见；等到 bat 里的 xcopy 报
    「拒绝访问」，应用已经关了，用户什么都看不到。
    """
    install = _make_install(tmp_path)
    exe = install / "MP Harvest.exe"
    monkeypatch.setattr(sys, "executable", str(exe))
    pkg = _make_zip(tmp_path, nested=False)

    def boom(self, *a, **k):
        raise PermissionError(13, "拒绝访问")

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(PlatformError) as ei:
        WinUpdater().apply(pkg)
    assert "不可写" in str(ei.value)


def test_apply_rejects_wrong_platform_package(frozen, monkeypatch, tmp_path, no_exit, no_popen):
    """拿到别的平台的包（或层级不对）时必须报错，不能把应用杀了再装一堆垃圾。"""
    install = _make_install(tmp_path)
    monkeypatch.setattr(sys, "executable", str(install / "MP Harvest.exe"))
    pkg = tmp_path / "MP-Harvest-mac-9.9.9.zip"
    with zipfile.ZipFile(pkg, "w") as zf:
        zf.writestr("MP Harvest.app/Contents/MacOS/MP Harvest", b"mac")
    with pytest.raises(PlatformError) as ei:
        WinUpdater().apply(pkg)
    assert "更新包内容不对" in str(ei.value)


@pytest.mark.parametrize("nested", [True, False])
def test_apply_writes_bat_and_launches_it(
    frozen, monkeypatch, tmp_path, no_exit, no_popen, nested
):
    """两种 zip 布局都要生成指向**正确载荷**的脚本，并按 OEM 编码落盘。"""
    install = _make_install(tmp_path)
    monkeypatch.setattr(sys, "executable", str(install / "MP Harvest.exe"))
    pkg = _make_zip(tmp_path, nested=nested)

    fake_ctypes = types.ModuleType("ctypes")
    fake_ctypes.windll = types.SimpleNamespace(
        kernel32=types.SimpleNamespace(GetOEMCP=lambda: 936)
    )
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    monkeypatch.setattr(paths_mod, "data_dir", lambda: tmp_path / "data")

    WinUpdater().apply(pkg)

    script = tmp_path / "data" / "update" / "apply_update.bat"
    text = script.read_text(encoding="cp936")
    # 载荷必须是解压目录里**那一层**：有顶层文件夹时下沉进去，平铺时用它本身。
    # 搞错就是 `安装目录/MP Harvest/MP Harvest.exe` 这种套娃 —— 更新"成功"、版本没变。
    extracted = tmp_path / "extracted"
    expected_payload = (extracted / "MP Harvest") if nested else extracted
    assert f'xcopy /E /Y /I "{expected_payload}"' in text
    assert f'"{expected_payload}" "{install}"' in text
    assert "tasklist" in text
    assert "timeout " not in text
    assert text.count("\r\r\n") == 0
    assert no_popen and no_popen[0][:2] == ["cmd", "/c"]


def test_apply_clears_previous_extraction(frozen, monkeypatch, tmp_path, no_exit, no_popen):
    """上一版解压残留必须先清掉。

    ``extractall`` 是**叠加**的，两版文件混在一起，下一次 xcopy 就把两份安装
    搅成一份谁也认不出的东西。
    """
    install = _make_install(tmp_path)
    monkeypatch.setattr(sys, "executable", str(install / "MP Harvest.exe"))
    monkeypatch.setattr(paths_mod, "data_dir", lambda: tmp_path / "data")
    stale = tmp_path / "extracted" / "leftover-from-old-version.dll"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"junk")

    WinUpdater().apply(_make_zip(tmp_path, nested=False))
    assert not stale.exists()
