from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core.article_reader import batch_export_articles  # noqa: E402


def test_batch_export_writes_html_files_and_index():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)

        articles = [
            {"title": "第一篇", "link": "https://mp.weixin.qq.com/s/a1", "keep": True, "reason": "技术深度好"},
            {"title": "第二篇", "link": "https://mp.weixin.qq.com/s/a2", "keep": False, "reason": "商业新闻"},
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
        )
        assert result["ok"] == 2
        assert result["failed"] == 0
        assert result["fmt"] == "html"
        # 逐篇 HTML
        html_files = [p for p in out.glob("*.html") if p.name != "index.html"]
        assert len(html_files) == 2
        assert all(p.read_text(encoding="utf-8").startswith("<!doctype html>") for p in html_files)
        # index.html 目录页
        index = Path(result["index"])
        assert index.name == "index.html" and index.is_file()
        text = index.read_text(encoding="utf-8")
        assert "标题-a1" in text and "标题-a2" in text
        assert "2026-08-05 10:00" in text
        # titles_filtered 风格说明页（2026-08-09）：账号/判定/本地与原文链接/筛选排序
        assert "测试号" in text
        assert "通过" in text and "过滤掉" in text
        assert "技术深度好" in text
        assert "本地HTML" in text and "原文" in text
        assert 'id="filter"' in text and 'data-key="date"' in text


def test_batch_export_counts_failures():
    with tempfile.TemporaryDirectory() as td:
        articles = [
            {"title": "无链接篇", "link": ""},
            {"title": "正常篇", "link": "https://mp.weixin.qq.com/s/a2"},
        ]

        def fake_fetch(url: str, cred=None):
            return {"title": "T", "link": url, "body_html": "<p>x</p>", "publish_at": ""}

        result = batch_export_articles(articles, out_dir=td, fetch_article=fake_fetch)
        assert result["ok"] == 1
        assert result["failed"] == 1
        assert result["errors"]
