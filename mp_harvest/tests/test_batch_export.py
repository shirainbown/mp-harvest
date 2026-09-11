from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core.article_reader import batch_export_articles  # noqa: E402
from mp_harvest.core.export_records import ExportRecords  # noqa: E402


def test_article_template_resolves():
    """回归：打包漏模板会导出全失败（v2.0.4/v2.0.5 事故），模板必须可定位。"""
    from mp_harvest.core import article_reader

    assert article_reader._TEMPLATE_DIR.is_dir()
    assert (article_reader._TEMPLATE_DIR / "article.html").is_file()


def test_safe_export_filename_date_account_index():
    from mp_harvest.core.article_reader import safe_export_filename

    # content_hash 优先于批次序号（2026-09 重构 B6：文件名不含批次内序号）
    name = safe_export_filename(
        "标题<A>/带斜杠",
        ext="html",
        index=3,
        date="2026-08-05 10:00",
        account="测试号",
        content_hash="abcdef0123456789",
    )
    assert name == "2026-08-05_测试号_标题_A_带斜杠_abcdef01.html"
    # 缺日期/公众号时退化为 标题_hash
    assert safe_export_filename("标题", ext="html", content_hash="ff" * 16) == "标题_ffffffff.html"
    # 旧调用兼容：无 content_hash 时仍可用编号
    assert safe_export_filename("标题", ext="md", index=1) == "01_标题.md"


def test_batch_export_writes_html_files_and_index():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        records = ExportRecords(out / "export_records.db")

        articles = [
            {"title": "第一篇", "link": "https://mp.weixin.qq.com/s/a1", "keep": True, "reason": "技术深度好",
             "publish_at": "2026-08-05 10:00", "_account_id": "acc1", "account": "测试号"},
            {"title": "第二篇", "link": "https://mp.weixin.qq.com/s/a2", "keep": False, "reason": "商业新闻",
             "publish_at": "2026-08-05 10:00", "_account_id": "acc1", "account": "测试号"},
        ]

        def fake_fetch(url: str, cred=None):
            return {
                "title": "标题-" + url[-2:],
                "link": url,
                "body_text": "正文内容",
                "body_html": "<p>正文内容</p>",
                "publish_at": "2026-08-05 10:00",
                "publish_ts": 1785750000,
            }

        result = batch_export_articles(
            articles,
            out_dir=out,
            fetch_article=fake_fetch,
            cred=None,
            account_name="测试号",
            records=records,
        )
        assert result["ok"] is True
        assert result["partial"] is False
        assert result["exported"] == 2
        assert result["skipped"] == 0
        assert result["failed"] == 0
        assert result["fmt"] == "html"
        # 逐篇 HTML
        html_files = [p for p in out.glob("*.html") if p.name != "index.html"]
        assert len(html_files) == 2
        assert all(p.read_text(encoding="utf-8").startswith("<!doctype html>") for p in html_files)
        # 文件名：日期_公众号_行标题_hash8（拉取前确定，幂等；不含批次序号，B6）
        names = sorted(p.name for p in html_files)
        assert names[0].startswith("2026-08-05_测试号_")
        assert "第一篇" in names[0] and "第二篇" in names[1]
        h1 = hashlib.sha256("https://mp.weixin.qq.com/s/a1".encode()).hexdigest()[:8]
        h2 = hashlib.sha256("https://mp.weixin.qq.com/s/a2".encode()).hexdigest()[:8]
        assert names[0].endswith(f"_{h1}.html") and names[1].endswith(f"_{h2}.html")
        assert "_01_" not in names[0] and "_02_" not in names[1]
        # index.html 目录页（从导出记录库生成）
        index = Path(result["index"])
        assert index.name == "index.html" and index.is_file()
        text = index.read_text(encoding="utf-8")
        assert "标题-a1" in text and "标题-a2" in text
        # titles_filtered 风格说明页：账号/判定/本地与原文链接/筛选排序
        assert "测试号" in text
        assert "通过" in text and "过滤掉" in text
        assert "技术深度好" in text
        assert "本地HTML" in text and "原文" in text
        assert 'id="filter"' in text and 'data-key="date"' in text


def test_batch_export_is_idempotent_across_batches():
    """同一篇文章换批次重跑：文件名不变、跳过 HTTP 拉取（B6）。"""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        records = ExportRecords(out / "export_records.db")
        calls: list[str] = []

        def fake_fetch(url: str, cred=None):
            calls.append(url)
            return {"title": "T", "link": url, "body_html": "<p>x</p>", "publish_at": "2026-08-05 10:00"}

        row = {"title": "重复篇", "link": "https://mp.weixin.qq.com/s/a1", "_account_id": "acc1", "account": "号A"}
        r1 = batch_export_articles([row], out_dir=out, fetch_article=fake_fetch, records=records)
        assert r1["exported"] == 1
        # 第二个批次（不同视图位置 + 新文章）再导同一篇：文件名不变、跳过拉取
        r2 = batch_export_articles(
            [row, {"title": "新篇", "link": "https://mp.weixin.qq.com/s/b1",
             "_account_id": "acc1", "account": "号A"}],
            out_dir=out,
            fetch_article=fake_fetch,
            records=records,
        )
        assert r2["exported"] == 1 and r2["skipped"] == 1
        assert calls == ["https://mp.weixin.qq.com/s/a1", "https://mp.weixin.qq.com/s/b1"]  # a1 未重复拉取
        html_files = [p for p in out.glob("*.html") if p.name != "index.html"]
        assert len(html_files) == 2  # 不产生副本
        # 文件被删后重跑：重新拉取（remove_missing 语义）
        (out / f"{r1['written'][0].split('/')[-1]}").unlink()
        records.remove_missing()
        r3 = batch_export_articles([row], out_dir=out, fetch_article=fake_fetch, records=records)
        assert r3["exported"] == 1 and r3["skipped"] == 0


def test_batch_export_index_accumulates_across_batches():
    """目录页跨批次累积：第二批导出后 index.html 仍含第一批文章（B7）。"""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        records = ExportRecords(out / "export_records.db")

        def fake_fetch(url: str, cred=None):
            return {"title": "T-" + url[-2:], "link": url, "body_html": "<p>x</p>", "publish_at": "2026-08-01 08:00"}

        batch_export_articles(
            [{"title": "第一批", "link": "https://mp.weixin.qq.com/s/a1", "_account_id": "acc1", "account": "号A"}],
            out_dir=out, fetch_article=fake_fetch, records=records,
        )
        result = batch_export_articles(
            [{"title": "第二批", "link": "https://mp.weixin.qq.com/s/b1", "_account_id": "acc1", "account": "号A"}],
            out_dir=out, fetch_article=fake_fetch, records=records,
        )
        text = Path(result["index"]).read_text(encoding="utf-8")
        assert "T-a1" in text and "T-b1" in text


def test_batch_export_cancellation_writes_partial_index():
    """取消导出：已完成部分保留且 index.html 已生成，响应 ok=False + partial（B9）。"""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        records = ExportRecords(out / "export_records.db")

        def fake_fetch(url: str, cred=None):
            return {"title": "T", "link": url, "body_html": "<p>x</p>", "publish_at": "2026-08-05 10:00"}

        state = {"n": 0}

        def check_cancelled():
            state["n"] += 1
            if state["n"] > 1:
                raise RuntimeError("cancelled")

        result = batch_export_articles(
            [
                {"title": "一", "link": "https://mp.weixin.qq.com/s/a1", "_account_id": "acc1", "account": "号A"},
                {"title": "二", "link": "https://mp.weixin.qq.com/s/a2", "_account_id": "acc1", "account": "号A"},
                {"title": "三", "link": "https://mp.weixin.qq.com/s/a3", "_account_id": "acc1", "account": "号A"},
            ],
            out_dir=out,
            fetch_article=fake_fetch,
            check_cancelled=check_cancelled,
            records=records,
        )
        assert result["ok"] is False
        assert result["partial"] is True
        assert result["exported"] == 1  # 第一篇已完成
        index = Path(result["index"])
        assert index.is_file()
        assert "T" in index.read_text(encoding="utf-8")


def test_batch_export_counts_failures():
    with tempfile.TemporaryDirectory() as td:
        records = ExportRecords(Path(td) / "export_records.db")
        articles = [
            {"title": "无链接篇", "link": ""},
            {"title": "正常篇", "link": "https://mp.weixin.qq.com/s/a2"},
            {"title": "异常篇", "link": "https://mp.weixin.qq.com/s/a3"},
        ]

        def fake_fetch(url: str, cred=None):
            if url.endswith("a3"):
                raise ValueError("拉取失败")
            return {"title": "T", "link": url, "body_html": "<p>x</p>", "publish_at": ""}

        result = batch_export_articles(
            articles, out_dir=td, fetch_article=fake_fetch, records=records
        )
        assert result["ok"] is True  # 单篇失败不中断整批、不影响 ok
        assert result["exported"] == 1
        assert result["failed"] == 2
        assert any("无链接" in e for e in result["errors"])
        assert any("拉取失败" in e for e in result["errors"])


def test_batch_export_out_dir_is_absolute():
    """out_dir 必须是绝对路径字符串。"""
    with tempfile.TemporaryDirectory() as td:
        records = ExportRecords(Path(td) / "export_records.db")
        import os

        cwd = os.getcwd()
        try:
            os.chdir(td)
            expected = os.path.join(os.getcwd(), "relative-out")
            result = batch_export_articles(
                [{"title": "t", "link": "https://mp.weixin.qq.com/s/a1"}],
                out_dir="relative-out",
                fetch_article=lambda url, cred=None: {"title": "t", "link": url, "body_html": "<p>x</p>"},
                records=records,
            )
        finally:
            os.chdir(cwd)
        assert os.path.isabs(result["out_dir"])
        assert result["out_dir"] == expected
