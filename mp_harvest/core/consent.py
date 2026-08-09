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


def _ask_native() -> bool:
    """原生确认框（tkinter 标准库）；tkinter 不可用时退回控制台输入。"""
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
    except Exception:
        try:
            answer = input("同意免责声明请输 y，否则直接回车：").strip().lower()
            return answer in ("y", "yes")
        except Exception:
            return False


def require_consent(
    ask: Callable[[], bool] | None = None,
    data_dir: Path | None = None,
    root: Path | None = None,
) -> bool:
    """启动门禁：已同意→True；已阻止→False；未表态→弹窗确认并记录。"""
    if is_blocked(data_dir=data_dir, root=root):
        return False
    if load_consent(data_dir) == "agreed":
        return True
    fn = ask or _ask_native
    agreed = bool(fn())
    save_consent("agreed" if agreed else "blocked", data_dir=data_dir, root=root)
    return agreed
