from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core.article_reader import (  # noqa: E402
    parse_wechat_article_html,
    render_article_html,
)

SAMPLE_HTML = """
<html><head>
<meta property="og:title" content="测试标题">
<meta property="og:description" content="摘要一段">
</head><body>
<h1 class="rich_media_title" id="activity-name">测试标题</h1>
<div id="js_content" class="rich_media_content">
<p>第一段内容。</p>
<p>第二段<strong>加粗</strong>。</p>
</div>
<script>var ct = "1785751249";</script>
</body></html>
"""


def test_parse_wechat_article_html():
    art = parse_wechat_article_html(SAMPLE_HTML, source_url="https://mp.weixin.qq.com/s/abc")
    assert art["title"] == "测试标题"
    assert "第一段内容" in art["body_text"]
    assert "js_content" in art["body_html"] or "第一段" in art["body_html"]
    assert art["link"] == "https://mp.weixin.qq.com/s/abc"


def test_render_article_html_document():
    art = parse_wechat_article_html(SAMPLE_HTML, source_url="https://mp.weixin.qq.com/s/abc")
    doc = render_article_html(art)
    assert "<!doctype html>" in doc.lower()
    assert "<title>测试标题</title>" in doc
    assert "第一段内容" in doc
    assert "<strong>加粗</strong>" in doc
    # meta 行包含原文链接
    assert '<a href="https://mp.weixin.qq.com/s/abc"' in doc
    # 正文导出只有 HTML，模板内联 CSS + 暗色媒体查询
    assert "prefers-color-scheme:dark" in doc
