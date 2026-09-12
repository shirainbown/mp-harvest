"""文件名净化里**跨平台**的那一半（2026-09 Windows 版补齐）。

非法字符 ``\\/:*?"<>|`` 的替换仍然留在各自调用点的正则里 —— 那两处的字符集本来
就不一样（导出文件名允许空格，条目 id 不允许），合并会改变已有的文件名。这个模块
只补 Windows 特有的两条，让同一个条目的文件名在 mac 与 Windows 上完全一致：

1. **保留设备名**：``CON``/``PRN``/``AUX``/``NUL``/``COM1-9``/``LPT1-9`` 在 Windows
   上是设备不是文件，而且**带扩展名也照样是设备**（``NUL.txt`` 等价于 ``NUL``）。
   一个恰好叫 ``con`` 的公众号、或者一条 id 是 ``aux`` 的条目，落盘时会失败或写进
   一个黑洞。
2. **结尾的点与空格**：Windows 会**静默**去掉它们 —— 「我们以为写出去的名字」和
   「磁盘上真正的名字」于是不一致，下次按名字去找就找不到了。

只在**整个主干**上判定，不做子串替换：``CONVERT`` 是正常名字，不能动。
"""

from __future__ import annotations

import re

# 文件名里不能出现的字符。「其他来源」的条目 id 与周报归档都用这一套
# （连空白一起换掉）；文章导出文件名允许空格，用的是另一套，见
# article_reader.safe_export_filename。
UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\s]+')

# Windows 的保留设备名（大小写不敏感）。COM0/LPT0 不是设备，所以从 1 开始。
WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


def windows_safe_stem(stem: str) -> str:
    """把一个文件名主干修成「两个平台都能真的落盘、且落盘后名字不变」。

    返回空串表示净化后什么都不剩，由调用方决定兜底值（各处的兜底并不一样）。
    """
    text = str(stem or "").rstrip(". ")
    if not text:
        return ""
    # 带扩展名也算设备名（NUL.txt 等价于 NUL），所以只看第一个点之前
    if text.split(".", 1)[0].strip().upper() in WINDOWS_RESERVED:
        return f"_{text}"
    return text


def safe_stem(text: str, *, max_len: int = 80) -> str:
    """条目 id / arXiv 编号 → 文件名主干（非法字符与空白换成 ``_``）。

    统一 ``external_sources.safe_id``、``external_sources.body_filename`` 与
    ``weekly_report`` 归档文件名这三处 —— 它们本来各写了一遍同样的正则，而且
    **已经漂移了**：周报那处没有截断，同一篇 arXiv 文章经「其他来源」导出与经
    周报归档会得到两个不同的文件名（2026-09 合并时发现）。

    先截断再净化：截断有可能正好切出一个结尾的点或空格。
    """
    cleaned = UNSAFE_FILENAME_RE.sub("_", str(text or "")).strip("_")[:max_len]
    return windows_safe_stem(cleaned)
