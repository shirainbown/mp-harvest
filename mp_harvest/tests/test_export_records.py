"""导出记录库（core/export_records.py）单元测试：幂等键、过滤、remove_missing、容错。"""

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


def test_remove_missing(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    f = out / "a.html"
    f.write_text("x", encoding="utf-8")
    r = _rec(tmp_path / "t.db")
    r.record_export(article_id="a1", out_path=str(f), exported_at=1)
    r.record_export(article_id="a2", out_path=str(out / "gone.html"), exported_at=2)
    assert r.remove_missing() == 1
    assert r.find_export("a2", str(out / "gone.html")) is None
    assert r.find_export("a1", str(f)) is not None
    # 文件删除后重跑可重新导出
    f.unlink()
    assert r.remove_missing() == 1
    assert r.list_exports() == []


def test_fault_tolerance_on_bad_path(tmp_path):
    """DB 路径不可写（这里是把 db_path 指向已存在的目录）：所有方法不抛异常。"""
    r = ExportRecords(tmp_path)  # 目录本身不能当 db 文件用
    assert r.record_export(article_id="a1", out_path="/x") is False
    assert r.find_export("a1", "/x") is None
    assert r.list_exports() == []
    assert r.remove_missing() == 0
