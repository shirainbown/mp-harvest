"""免责声明同意门禁（核心逻辑，无 GUI 依赖，便于测试）。

状态机：
  - 空      ：未表态 → 弹窗确认；同意→agreed，不同意→blocked
  - agreed  ：放行
  - blocked ：拒绝启动（静默退出；界面上只提示过"继续使用前需确认同意"）

阻止标记同时写入数据目录（权威）与安装目录（尽力而为，重装可清除），
任一存在即视为阻止。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

from mp_harvest.infra.platform import paths

STATE_FILE = "consent.json"
BLOCK_FILE = ".consent_blocked"


def state_path(data_dir: Path | None = None) -> Path:
    return (data_dir or paths.data_dir()) / STATE_FILE


def bundle_block_path(root: Path | None = None) -> Path:
    return (root or paths.package_root()) / BLOCK_FILE


def load_consent(data_dir: Path | None = None) -> str:
    p = state_path(data_dir)
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return str(data.get("status") or "")
    except Exception:
        return ""


def save_consent(
    status: str,
    data_dir: Path | None = None,
    root: Path | None = None,
) -> None:
    p = state_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"status": status}, ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(p)
    if status == "blocked":
        try:
            bundle_block_path(root).write_text("blocked", encoding="utf-8")
        except Exception:
            pass


def is_blocked(data_dir: Path | None = None, root: Path | None = None) -> bool:
    if load_consent(data_dir) == "blocked":
        return True
    try:
        return bundle_block_path(root).exists()
    except Exception:
        return False


DISCLAIMER_TEXT = (
    "本软件仅供个人学习与研究使用，严禁用于任何商业用途。\n\n"
    "· 本软件与微信、腾讯及其关联公司无任何关联、背书或赞助关系；\n"
    "· 使用本软件须遵守微信《软件许可及服务协议》及相关平台规则、法律法规；\n"
    "· 使用本软件产生的一切风险（账号异常、封禁、数据丢失、纠纷等）由使用者自行承担；\n"
    "· 作者及贡献者不对因使用本软件造成的任何直接或间接损失负责。\n\n"
    "继续使用前需阅读并确认同意以上声明。\n"
    "是否同意并继续？"
)


def _ask_windows() -> bool | None:
    """Windows 原生确认框（``user32.MessageBoxW``）；失败返回 None。

    **不用 tkinter**（2026-09 Windows 适配）：冻结版是 ``console=False`` 的 GUI 程序，
    tkinter 要额外带上 ``_tkinter`` 与 tcl/tk 的 DLL + 数据目录，任何一环缺失都会掉进
    下面的兜底分支 —— 而没有控制台时 ``input()`` 必然抛异常，于是「弹窗没弹出来」
    被当成「用户点了不同意」永久记进 blocked，应用从此再也起不来。
    ``MessageBoxW`` 直接来自系统 DLL，零打包成本、零额外体积。
    """
    try:
        import ctypes

        MB_YESNO = 0x04
        MB_ICONWARNING = 0x30
        IDYES = 6
        res = ctypes.windll.user32.MessageBoxW(
            None, DISCLAIMER_TEXT, "免责声明", MB_YESNO | MB_ICONWARNING
        )
        return res == IDYES
    except Exception:  # noqa: BLE001
        return None


def _ask_tk() -> bool | None:
    """tkinter 标准库确认框；不可用返回 None。"""
    try:
        from tkinter import messagebox

        root = None
        try:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
        except Exception:
            root = None
        try:
            return bool(messagebox.askyesno("免责声明", DISCLAIMER_TEXT, parent=root))
        finally:
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass
    except Exception:  # noqa: BLE001
        return None


def _ask_console() -> bool | None:
    """最后的兜底：在终端里问一句。没有终端（GUI 程序）时返回 None。"""
    try:
        answer = input("同意免责声明请输 y，否则直接回车：").strip().lower()
        return answer in ("y", "yes")
    except Exception:  # noqa: BLE001
        return None


def _ask_native() -> bool | None:
    """原生确认框；返回 ``None`` 表示**没能问成**（与「用户点了不同意」是两回事）。"""
    if sys.platform == "win32":
        answer = _ask_windows()
        if answer is not None:
            return answer
    answer = _ask_tk()
    if answer is not None:
        return answer
    return _ask_console()


def require_consent(
    ask: Callable[[], bool | None] | None = None,
    data_dir: Path | None = None,
    root: Path | None = None,
) -> bool:
    """启动门禁：已同意→True；已阻止→False；未表态→弹窗确认并记录。

    ⚠️ **「没问成」不等于「用户拒绝」**（2026-09 Windows 适配）：弹窗组件缺失、
    或者根本没有终端时，``_ask_native()`` 返回 ``None``。这种情况**绝不能**记成
    ``blocked`` —— 那会写下一个跨版本存活的永久标记（数据目录的 ``consent.json``
    与安装目录的 ``.consent_blocked`` 任一存在即视为阻止），应用从此静默退出、
    界面上一个字都不显示，用户无从知道该去删哪个文件。没问成就当次不放行，
    下次启动再问一遍。
    """
    if is_blocked(data_dir=data_dir, root=root):
        return False
    if load_consent(data_dir) == "agreed":
        return True
    fn = ask or _ask_native
    agreed = fn()
    if agreed is None:
        return False
    save_consent("agreed" if agreed else "blocked", data_dir=data_dir, root=root)
    return bool(agreed)
