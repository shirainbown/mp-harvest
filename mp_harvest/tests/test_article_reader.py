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


# ── 「分享」型消息的正文（2026-09 用户问「为什么会报错」）─────────────
#
# 微信里 digest 为「分享一篇文章。」的消息（转发别人的文章、群里的分享卡片），
# 它的 /s 页面**是个中转页**：#js_content 是空的占位 div，真正的正文在**另一个
# 公众号**的文章里 —— 页面上那个「阅读全文」按钮（#js_share_source）的 data-url
# 才是原文地址。
#
# 不跟进的话这类文章永远拿不到正文（解析出来 0 字），下游报「正文过短或无实质
# 内容」；用户拿着一句「正文过短」来问为什么 —— 而正文根本不在这页上。

# 结构与真实中转页一致（实测 2026-09）：空的 js_content + original_panel_tool
SHARE_PAGE = """
<html><head><meta property="og:title" content="转发示例文章"></head><body>
<div class="original_panel">
  <div class="original_panel_content" id="js_content" aria-hidden="true"></div>
  <div class="original_panel_tool" aria-hidden="true">
    <span data-url="http://mp.weixin.qq.com/s?__biz=SHAREBIZ%3D%3D&amp;mid=1000000001&amp;idx=1&amp;sn=share0000&amp;scene=45#wechat_redirect"
          class="weui-link" id="js_share_source">阅读全文</span>
  </div>
</div></body></html>
"""

REAL_ARTICLE = """
<html><head><meta property="og:title" content="转发示例文章"></head><body>
<h1 id="activity-name">转发示例文章</h1>
<div id="js_content"><p>这是原文页里的正文，讲的是从 FinFET 到 GAA 的晶体管演进。</p></div>
</body></html>
"""


def test_share_source_url_from_relay_page():
    from mp_harvest.core.article_reader import share_source_url

    url = share_source_url(SHARE_PAGE)
    # &amp; 必须还原成 &，否则这一跳抓的是个坏 URL
    assert url.startswith("http://mp.weixin.qq.com/s?__biz=SHAREBIZ%3D%3D&mid=1000000001")
    assert "&amp;" not in url


def test_share_source_url_ignores_normal_page_and_external_links():
    """正常文章页没有这个节点；指向站外的链接一律不跟进。"""
    from mp_harvest.core.article_reader import share_source_url

    assert share_source_url(REAL_ARTICLE) == ""
    assert share_source_url("") == ""
    external = '<span id="js_share_source" data-url="https://evil.example/x"></span>'
    assert share_source_url(external) == "", "站外链接不能跟进（页面内容不该决定我们请求谁）"


def test_fetch_follows_share_link_to_real_article(monkeypatch):
    """中转页 → 跟进「阅读全文」→ 拿到原文正文。"""
    from mp_harvest.core import article_reader as ar

    asked = []

    def fake_fetch(url, *, cred=None, timeout=25.0):
        asked.append(url)
        return SHARE_PAGE if len(asked) == 1 else REAL_ARTICLE

    monkeypatch.setattr(ar, "fetch_article_html", fake_fetch)
    out = ar.fetch_and_parse_article("https://mp.weixin.qq.com/s?__biz=B&mid=1&idx=1&sn=x")
    assert len(asked) == 2, "应当正好跟一跳"
    assert "FinFET 到 GAA" in out["body_text"], out["body_text"]
    assert out["link"].startswith("http://mp.weixin.qq.com/s?__biz=SHAREBIZ"), out["link"]
    # 记下中转页地址，排查时能看出这篇的正文是「跟过来的」
    assert out["share_relay_url"].endswith("sn=x")
    assert out["share_target_url"] == out["link"]


def test_normal_article_is_not_fetched_twice(monkeypatch):
    """正文本来就有 → 不做第二次请求（正常文章页里也可能有 #js_share_source）。"""
    from mp_harvest.core import article_reader as ar

    asked = []

    def fake_fetch(url, *, cred=None, timeout=25.0):
        asked.append(url)
        return REAL_ARTICLE

    monkeypatch.setattr(ar, "fetch_article_html", fake_fetch)
    ar.fetch_and_parse_article("https://mp.weixin.qq.com/s?__biz=B&mid=1&idx=1&sn=x")
    assert len(asked) == 1


def test_share_follow_failure_reports_why(monkeypatch):
    """跟不进去时不能把中转页当成功，而且要留下「这是转发型消息」的线索。"""
    from mp_harvest.core import article_reader as ar

    def fake_fetch(url, *, cred=None, timeout=25.0):
        if "sn=x" in url:
            return SHARE_PAGE
        raise RuntimeError("网络断了")

    monkeypatch.setattr(ar, "fetch_article_html", fake_fetch)
    out = ar.fetch_and_parse_article("https://mp.weixin.qq.com/s?__biz=B&mid=1&idx=1&sn=x")
    assert len(str(out["body_text"]).strip()) < 20, "不能谎报成功"
    assert out.get("share_target_url"), "要留下原文地址，报错才解释得清"
    reason = ar.body_failure_reason(out)
    assert "转发" in reason and "正文过短" not in reason, reason


def test_body_failure_reason_covers_the_three_cases():
    """三种「没正文」的解释各不相同 —— 只有第一种值得用户去重试。"""
    from mp_harvest.core.article_reader import body_failure_reason

    assert "环境校验" in body_failure_reason({"content_found": False})
    assert "转发" in body_failure_reason({"content_found": True, "share_target_url": "x"})
    assert body_failure_reason({"content_found": True}) == "正文过短或无实质内容"


# 上面那组用例盯的是「会不会跟进」；下面这组盯「什么时候**不该**跟进」——
# 三个变异体（够长也跟、不看好坏、自己指自己也跟）都从这组才抓得住。

# 正常文章页 + 转载关系里的 #js_share_source：有链接，但正文本来就完整
ARTICLE_WITH_SHARE_LINK = """
<html><body>
<h1 id="activity-name">有正文也有分享链接</h1>
<div id="js_content"><p>这一页本来就有完整正文，讲的是 AXI4-Stream 的边界设计取舍。</p></div>
<span id="js_share_source" data-url="https://mp.weixin.qq.com/s?__biz=OTHER&amp;mid=9"></span>
</body></html>
"""


def test_full_body_is_never_followed_even_with_share_link(monkeypatch):
    """正文够长就不跟进 —— 正常文章页也可能带 #js_share_source（原创转载关系），
    那些页面多打一次请求纯属浪费，还可能把正文换成转载源那篇的。"""
    from mp_harvest.core import article_reader as ar

    asked = []

    def fake_fetch(url, *, cred=None, timeout=25.0):
        asked.append(url)
        return ARTICLE_WITH_SHARE_LINK

    monkeypatch.setattr(ar, "fetch_article_html", fake_fetch)
    out = ar.fetch_and_parse_article("https://mp.weixin.qq.com/s?__biz=B&mid=1&idx=1&sn=x")
    assert len(asked) == 1, f"不该多发请求：{asked}"
    assert "AXI4-Stream" in out["body_text"]


def test_empty_target_does_not_replace_the_page(monkeypatch):
    """原文那页**也没有正文**（也是个空容器/中转页）→ 保留原结果并留下线索。

    中转页本身是 0 字，所以「谁更长」这个比较挡住的正是这种「跟过去还是空」的
    情况；不挡的话会把一个同样空的页面当成原文，还标记成跟进成功 ——
    报错就从「转发型消息，原文没取到」退化成没头没脑的「正文过短」。
    """
    from mp_harvest.core import article_reader as ar

    empty_article = '<html><body><div id="js_content"></div></body></html>'

    def fake_fetch(url, *, cred=None, timeout=25.0):
        return SHARE_PAGE if "sn=x" in url else empty_article

    monkeypatch.setattr(ar, "fetch_article_html", fake_fetch)
    out = ar.fetch_and_parse_article("https://mp.weixin.qq.com/s?__biz=B&mid=1&idx=1&sn=x")
    assert out["link"].endswith("sn=x"), "不该把中转页换成另一个同样空的页面"
    assert out.get("share_target_url"), "要留下原文地址这条线索"
    assert "转发" in ar.body_failure_reason(out), ar.body_failure_reason(out)


def test_self_referencing_share_link_is_not_refetched(monkeypatch):
    """中转页指向自己时不重复请求（否则就是一个白打的请求 + 递归风险）。"""
    from mp_harvest.core import article_reader as ar

    self_url = "https://mp.weixin.qq.com/s?__biz=B&mid=1&idx=1&sn=x"
    page = SHARE_PAGE.replace(
        "http://mp.weixin.qq.com/s?__biz=SHAREBIZ%3D%3D&amp;mid=1000000001&amp;idx=1&amp;sn=share0000&amp;scene=45#wechat_redirect",
        self_url,
    )
    asked = []

    def fake_fetch(url, *, cred=None, timeout=25.0):
        asked.append(url)
        return page

    monkeypatch.setattr(ar, "fetch_article_html", fake_fetch)
    ar.fetch_and_parse_article(self_url)
    assert len(asked) == 1, f"自己指自己不该再打一次：{asked}"
