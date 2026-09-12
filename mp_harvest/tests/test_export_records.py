"""导出记录库（core/export_records.py）单元测试：幂等键、过滤、已导出集合、容错。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core.export_records import ExportRecords  # noqa: E402


def _rec(db: Path) -> ExportRecords:
    return ExportRecords(db)


def test_record_and_find_roundtrip(tmp_path):
    r = _rec(tmp_path / "t.db")
    assert r.find_export("a1", "/out/x.html") is None
    assert r.record_export(
        article_id="a1", out_path="/out/x.html", sha256="abc", account_id="acc1",
        account_name="号A", title="标题", link="https://x", publish_ts=123,
        exported_at=1000, bytes_count=42,
    ) is True
    row = r.find_export("a1", "/out/x.html")
    assert row is not None
    assert row["article_id"] == "a1"
    assert row["account_id"] == "acc1"
    assert row["account_name"] == "号A"
    assert row["title"] == "标题"
    assert row["sha256"] == "abc"
    assert row["exported_at"] == 1000
    assert row["bytes"] == 42
    # 主键 (article_id, out_path)：同文章同路径覆盖，不同路径共存
    r.record_export(article_id="a1", out_path="/out/x.html", exported_at=2000)
    assert r.find_export("a1", "/out/x.html")["exported_at"] == 2000
    r.record_export(article_id="a1", out_path="/out2/x.html", exported_at=3000)
    assert len(r.list_exports()) == 2


def test_list_exports_filter_by_account_and_dir(tmp_path):
    r = _rec(tmp_path / "t.db")
    r.record_export(article_id="a1", account_id="acc1", out_path="/out/a.html", exported_at=1)
    r.record_export(article_id="a2", account_id="acc2", out_path="/out/b.html", exported_at=2)
    r.record_export(article_id="a3", account_id="acc1", out_path="/other/c.html", exported_at=3)
    assert len(r.list_exports()) == 3
    assert len(r.list_exports("acc1")) == 2
    assert len(r.list_exports("acc1", out_dir="/out")) == 1
    assert len(r.list_exports("acc2", out_dir="/out")) == 1
    assert len(r.list_exports(out_dir="/out")) == 2


def test_exported_article_ids_only_counts_files_that_still_exist(tmp_path):
    """**文件还在**才算「已导出」—— 用户报的那个 bug 的护栏。

    用户删掉导出的 HTML 之后，列表必须跟着变。若这里返回的是「有记录」的集合，
    界面就会一直挂着「已导出」而本地空空如也，比不显示更误导。

    （原 `remove_missing()` 靠删记录达到同样效果，但它从来没被调用过，而且是写
    操作、不该挂在读路径上 —— 删掉后由这条测试接管。）
    """
    out = tmp_path / "out"
    out.mkdir()
    f = out / "a.html"
    f.write_text("x", encoding="utf-8")
    r = _rec(tmp_path / "t.db")
    r.record_export(article_id="a1", out_path=str(f), exported_at=1)
    r.record_export(article_id="a2", out_path=str(out / "gone.html"), exported_at=2)

    assert r.exported_article_ids() == {"a1"}, "只该算文件还在的那条"
    # 记录仍在（读侧不写库），但集合里必须消失
    assert r.find_export("a2", str(out / "gone.html")) is not None
    f.unlink()
    assert r.exported_article_ids() == set(), "文件删光后一篇都不该算已导出"


def test_exported_article_ids_merges_repeat_exports(tmp_path):
    """同一篇导出过两次（换目录/改标题）只算一条 —— 与 find_by_article 同口径。"""
    out = tmp_path / "out"
    out.mkdir()
    a, b = out / "a.html", out / "b.html"
    a.write_text("x", encoding="utf-8")
    b.write_text("y", encoding="utf-8")
    r = _rec(tmp_path / "t.db")
    r.record_export(article_id="a1", out_path=str(a), exported_at=1)
    r.record_export(article_id="a1", out_path=str(b), exported_at=2)
    assert r.exported_article_ids() == {"a1"}
    # 删掉其中一份，另一份还在 → 仍算已导出
    a.unlink()
    assert r.exported_article_ids() == {"a1"}


def test_fault_tolerance_on_bad_path(tmp_path):
    """DB 路径不可写（这里是把 db_path 指向已存在的目录）：所有方法不抛异常。"""
    r = ExportRecords(tmp_path)  # 目录本身不能当 db 文件用
    assert r.record_export(article_id="a1", out_path="/x") is False
    assert r.find_export("a1", "/x") is None
    assert r.list_exports() == []
    assert r.exported_article_ids() == set()
