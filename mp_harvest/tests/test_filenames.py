"""文件名净化里 Windows 特有的那两条（2026-09）。

这些名字在 macOS 上**怎么写都成功**，所以只有把这些规则本身钉住，才可能在 mac 上
发现问题。症状在 Windows 上是：文件创建失败、或者写进去了但名字被系统悄悄改掉，
下一次按名字找不到。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core.article_reader import safe_export_filename  # noqa: E402
from mp_harvest.core.external_sources import body_filename, safe_id  # noqa: E402
from mp_harvest.core.filenames import safe_stem, windows_safe_stem  # noqa: E402


# ── windows_safe_stem ────────────────────────────────────────────


def test_reserved_device_names_get_prefixed():
    """CON/NUL/… 在 Windows 上是**设备**，不是文件；带扩展名也一样。"""
    for name in ("CON", "con", "NUL", "aux", "PRN", "COM1", "lpt9"):
        assert windows_safe_stem(name) == f"_{name}", name
    assert windows_safe_stem("NUL.html") == "_NUL.html"


def test_reserved_lookalikes_are_untouched():
    """只有**整个主干**恰好是保留字才算，CONVERT / COM10 是正常名字。"""
    for name in ("CONVERT", "COM10", "COM0", "NULS", "auxiliary", "MyNUL"):
        assert windows_safe_stem(name) == name, name


def test_trailing_dots_and_spaces_stripped():
    """Windows 会静默吃掉结尾的点和空格，于是磁盘上的名字与记录的不一致。"""
    assert windows_safe_stem("标题...") == "标题"
    assert windows_safe_stem("标题 ") == "标题"
    assert windows_safe_stem("标题. . ") == "标题"
    # 中间的点/空格是正常的，不能动
    assert windows_safe_stem("a.b c") == "a.b c"


def test_all_dots_becomes_empty():
    assert windows_safe_stem("...") == ""
    assert windows_safe_stem("   ") == ""
    assert windows_safe_stem("") == ""


# ── safe_stem（条目 id / arXiv 编号）─────────────────────────────


def test_safe_stem_replaces_illegal_and_whitespace():
    assert safe_stem("2608.26575v1") == "2608.26575v1"
    assert safe_stem("a/b c:d") == "a_b_c_d"
    assert safe_stem("  x  ") == "x"


def test_safe_stem_truncates_then_sanitizes():
    """**先截断、再净化**，顺序反过来会漏掉一个结尾的点。

    ``"a"*79 + ".b"`` 截到 80 位正好是 ``"a"*79 + "."``，净化再把这个尾点去掉。
    如果反过来（先净化再截断），净化时那个点还在字符串中间、不会被去掉，
    截断之后才跑到末尾 —— 于是一个 Windows 会静默吃掉的名字就写出去了。
    """
    assert len(safe_stem("a" * 200)) == 80
    assert safe_stem("a" * 79 + ".b") == "a" * 79
    assert safe_stem("a" * 80 + ".") == "a" * 80


def test_safe_stem_reserved_name():
    assert safe_stem("CON") == "_CON"
    assert safe_stem("nul") == "_nul"


# ── 三个调用点都要走这套规则 ─────────────────────────────────────


def test_safe_id_reserved_name():
    assert safe_id("url:CON") == "_CON"


def test_body_filename_reserved_name():
    """body_filename 有 arXiv 编号时算出来是 {arxiv_id}.html，保留名要绕开。"""
    assert body_filename({"arxiv_id": "CON", "item_key": "arxiv:CON"}) == "_CON.html"
    assert body_filename({"arxiv_id": "aux", "item_key": "arxiv:aux"}) == "_aux.html"


def test_body_filename_empty_after_sanitize_falls_back():
    """编号净化后为空时退回 safe_id，不能写出一个只剩 ``.html`` 的隐藏文件。"""
    got = body_filename({"arxiv_id": "///", "item_key": "arxiv:2608.1"})
    assert got == "2608.1.html"


def test_export_filename_title_that_is_a_reserved_name():
    got = safe_export_filename("CON", ext="html", date="2026-09-12", account="号A")
    assert got == "2026-09-12_号A__CON.html"


def test_export_filename_normal_title_unchanged():
    """常规标题一个字符都不能动 —— 改了会让老用户重跑导出时产生副本。"""
    got = safe_export_filename("台积电传", ext="html", date="2026-09-12", account="号A")
    assert got == "2026-09-12_号A_台积电传.html"
