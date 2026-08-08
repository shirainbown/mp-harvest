"""HTML 导出（设计稿 §6）测试：sanitize 白名单、模板渲染、跟踪参数剥离。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core.article_reader import (  # noqa: E402
    ARTICLE_EXPORT_FORMATS,
    parse_wechat_article_html,
    render_article_html,
    sanitize_article_html,
    write_article_export,
)

RAW = """
<html><body class="zh_CN">
<div id="js_article">
  <h1 id="activity-name">导出测试标题</h1>
  <div id="js_top_ad_area">广告</div>
  <div id="js_content" style="visibility:hidden;opacity:0">
    <p>第一段。</p>
    <p><img data-src="https://mmbiz.qpic.cn/a.jpg?wx_fmt=jpeg" /></p>
    <p>第二段<strong>加粗</strong>。</p>
    <a href="https://mp.weixin.qq.com/s?__biz=B&mid=1&idx=1&chksm=xx&scene=27#rd">内链</a>
    <script>alert(1)</script>
    <iframe src="https://x.example/embed"></iframe>
    <span onclick="track()">装饰</span>
  </div>
  <div id="js_pc_qr_code">二维码</div>
  <script>var ct = "1785751249";</script>
</div>
</body></html>
"""


def test_only_html_export_format():
    # 正文导出只有 HTML（docx/markdown/txt 分支已删除）
    assert ARTICLE_EXPORT_FORMATS == {"html": "HTML"}


def test_sanitize_removes_noise_and_unhides_content():
    out = sanitize_article_html(RAW)
    assert "<script" not in out
    assert "<iframe" not in out
    assert "onclick" not in out
    assert "visibility:hidden" not in out  # js_content 取消隐藏
    assert "第一段" in out
    assert "<strong>加粗</strong>" in out


def test_sanitize_promotes_data_src_and_no_referrer():
    out = sanitize_article_html(RAW)
    assert 'src="https://mmbiz.qpic.cn/a.jpg?wx_fmt=jpeg"' in out
    assert "data-src" not in out
    assert 'referrerpolicy="no-referrer"' in out


def test_sanitize_strips_tracking_params():
    out = sanitize_article_html(RAW)
    assert "chksm" not in out
    assert "scene=27" not in out
    # 关键参数保留
    assert "__biz=B" in out and "mid=1" in out


def test_sanitize_drops_javascript_href():
    out = sanitize_article_html('<a href="javascript:alert(1)">x</a>')
    assert "javascript:" not in out


def test_render_article_html_meta_line():
    art = parse_wechat_article_html(RAW, source_url="https://mp.weixin.qq.com/s/abc")
    doc = render_article_html(art, account="测试号")
    assert "<!doctype html>" in doc.lower()
    assert "<title>导出测试标题 - 测试号</title>" in doc
    # meta 行：公众号 · 发布时间 · 原文
    assert 'class="meta"' in doc
    assert "测试号 · " in doc
    assert '<a href="https://mp.weixin.qq.com/s/abc"' in doc
    # 模板自包含：内联 CSS + 暗色媒体查询
    assert "<style>" in doc and "prefers-color-scheme:dark" in doc
    # 正文已 sanitize
    assert "<script" not in doc and "<iframe" not in doc
    assert "js_top_ad_area" not in doc and "js_pc_qr_code" not in doc


def test_render_article_html_falls_back_to_body_text():
    art = {"title": "纯文本", "body_text": "只有文字", "body_html": "", "link": ""}
    doc = render_article_html(art)
    assert "<pre>" in doc and "只有文字" in doc


def test_write_article_export():
    import tempfile

    art = parse_wechat_article_html(RAW, source_url="https://mp.weixin.qq.com/s/abc")
    with tempfile.TemporaryDirectory() as td:
        out = write_article_export(Path(td) / "a.html", art, account="测试号")
        assert out.is_file()
        text = out.read_text(encoding="utf-8")
        assert "导出测试标题" in text
