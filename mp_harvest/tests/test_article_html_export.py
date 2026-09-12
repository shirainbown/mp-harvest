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


# ── 被丢掉的标签里还有子标签（2026-09 用户报的单篇导出失败）──────────
#
# 症状：整批导出里单篇失败，错误只有一句
# ``'NoneType' object has no attribute 'get'``。
#
# 根因在 bs4 的 ``decompose()``：它把节点 ``__dict__.clear()`` 并把 ``name``
# 设成**空字符串**（不是 None），于是清洗循环里那条「已随父节点被丢掉」的守卫
# （``tag.name is None``）整条失效，紧接着 ``tag.get("id")`` 就炸。
#
# 触发条件是「被丢掉的标签里有子标签」：``<script>`` 的内容在 html.parser 里
# 是纯文本，所以一直没暴露；``<svg>``（正文里的矢量图，含 <path>/<ellipse>）
# 是普通元素，子标签照常解析 —— 一篇带矢量图的文章必现。

_SVG_THEN_TEXT = """
<div id="js_content">
  <p>图前</p>
  <svg viewBox="0 0 10 10"><path d="M0 0"/><ellipse cx="1" cy="1"/></svg>
  <p>图后</p>
</div>
"""


def test_sanitize_survives_dropped_tag_with_children():
    """丢弃 <svg> 之后不能因为访问它的子标签而抛异常。"""
    out = sanitize_article_html(_SVG_THEN_TEXT)
    assert "<svg" not in out and "<path" not in out, out
    # 正文照常保留：出错点后面的内容不能跟着丢
    assert "图前" in out and "图后" in out


def test_sanitize_survives_dropped_id_with_children():
    """按 id 丢掉的容器里带子标签，同样不能炸（DROP_IDS 那条路）。"""
    out = sanitize_article_html(
        '<div id="js_pc_qr_code"><p>二维码</p><span><b>嵌套</b></span></div><p>正文</p>'
    )
    assert "二维码" not in out and "嵌套" not in out
    assert "正文" in out


def test_sanitize_survives_unwrapped_tag_with_children():
    """不在白名单里的标签会被 unwrap（保留子节点），子节点要照常处理。

    用 ``<header>``/``<nav>``：``<figure>`` 是白名单内的（别拿它测 unwrap，
    那样这条用例是空转的 —— 第一版就写错了）。
    """
    out = sanitize_article_html(
        "<header><p>段落</p><nav><img src='https://a/b.jpg'/></nav></header>"
    )
    assert "段落" in out
    assert "<img" in out, out
    assert "header" not in out and "nav" not in out, out


def test_sanitize_many_svgs_do_not_break_later_tags():
    """多个被丢掉的容器连着出现时，后面每个标签都要正常处理。

    守卫失效时的表现是「碰到第一个坏标签就整篇失败」，所以只测一个 svg 不够 ——
    要确认后续标签（含 img 的 src 改写）都还走完了流程。
    """
    html = (
        "<svg><path/></svg><svg><g/></svg>"
        "<p><img data-src='https://mmbiz.qpic.cn/x.jpg?wx_fmt=png'/></p>"
        "<a href='javascript:alert(1)'>坏链</a>"
    )
    out = sanitize_article_html(html)
    assert "svg" not in out
    assert 'src="https://mmbiz.qpic.cn/x.jpg?wx_fmt=png"' in out
    assert "javascript:" not in out
    assert out.count("<p>") == 1


def test_bs4_decompose_marks_descendants_with_empty_name():
    """钉住我们**依赖的 bs4 内部行为**：decompose 后子节点是 name="" + decomposed=True。

    这是上面那条守卫的前提。2026-09 之前守卫写的是 ``name is None`` —— 那是按
    旧版 bs4 写的，而 4.15 改成置**空字符串**，守卫整条失效、带 <svg> 的文章
    整篇导出失败（用户报的那句 `'NoneType' object has no attribute 'get'`）。

    bs4 一改这个行为，这条先红 —— 比等用户报「某篇文章导不出来」强。
    """
    from bs4 import BeautifulSoup, Tag

    soup = BeautifulSoup("<svg><path d='M0 0'/></svg>", "html.parser")
    svg = soup.find("svg")
    child = soup.find("path")
    assert isinstance(child, Tag)
    svg.decompose()
    assert child.decomposed is True, "bs4 不再标记 decomposed，守卫要换个写法"
    assert not child.name, f"子节点 name 不再是空/None，而是 {child.name!r}"
    # attrs 被清空正是崩的直接原因（.get 落在 None 上）
    assert child.attrs is None
