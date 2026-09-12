from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.infra.mitm.mitm_addon import (  # noqa: E402
    _enough,
    _merge_from_cookie,
    _merge_from_url,
    _url_carries_enough,
    extract_article_sighting,
    is_article_url,
)


def test_merge_url_and_enough():
    cred: dict[str, str] = {}
    url = (
        "https://mp.weixin.qq.com/s?__biz=Mzg3NTg3ODA5MA==&uin=123&key=abcdef"
        "&pass_ticket=pt"
    )
    assert _merge_from_url(url, cred)
    assert _enough(cred)
    assert _url_carries_enough(url)
    assert cred["__biz"] == "Mzg3NTg3ODA5MA=="


def test_merge_cookie():
    cred: dict[str, str] = {"__biz": "B"}
    assert _merge_from_cookie("uin=9; key=kk; pass_ticket=p", cred)
    assert cred["uin"] == "9"
    assert cred["key"] == "kk"
    assert _enough(cred)


def test_ignore_other_hosts():
    cred: dict[str, str] = {}
    assert not _merge_from_url("https://example.com/?__biz=B&uin=1&key=k", cred)
    assert not cred


def test_extract_article_sighting_short_and_query():
    assert is_article_url("https://mp.weixin.qq.com/s/e2QPPpQdnz48bWM0Uk9NyA")
    s = extract_article_sighting("https://mp.weixin.qq.com/s/e2QPPpQdnz48bWM0Uk9NyA")
    assert s is not None
    assert s["identity"] == "s:e2QPPpQdnz48bWM0Uk9NyA"
    s2 = extract_article_sighting(
        "https://mp.weixin.qq.com/s?__biz=B&mid=2&idx=1&sn=b#rd"
    )
    assert s2 is not None
    assert s2["identity"] == "mid:2|idx:1|sn:b"
    assert s2["__biz"] == "B"
    assert extract_article_sighting("https://mp.weixin.qq.com/mp/profile_ext?action=home") is None


# ── 从文章页 HTML 里取标题/发布时间（2026-09）────────────────────
#
# 背景：抓包目击只能拿到链接，标题和发布时间得从**文章页 HTML** 里扒。扒不到
# 不是错（目击行本来就可以没有这些），但**扒得到却因为正则太窄而漏掉**会被
# 用户当成数据坏了：列表里那篇文章永远没有发布时间，界面上看不出是抓取失败。


def _enrich(html: str) -> dict:
    from mp_harvest.infra.mitm.mitm_addon import _enrich_sighting_from_html

    return _enrich_sighting_from_html(html, {"title": "", "publish_ts": 0})


def test_publish_time_from_each_known_marker():
    """页面里有三处独立记录发布时间，任何一处能认出来就行。

    实测真文章页三处都在（``var ct`` / ``var create_time`` / ``var oriCreateTime``），
    但它们分布在文档的 88.6% / 88.6% / 45.6% —— 页面变体少掉任何一处都不奇怪。
    每一处都必须能单独认出来：只认第一处的话，剩下两种变体静默变成「没有时间」。
    """
    ts = 1785141617
    cases = {
        "var ct": f'var appmsg_type = "9";\nvar ct = "{ts}";\nvar user_name = "gh_x";',
        "var create_time": f'var nickname = "X";\nvar create_time = "{ts}";',
        "var oriCreateTime 单引号": f"var oriCreateTime = '{ts}';",
        "var oriCreateTime 双引号": f'var oriCreateTime = "{ts}";',
    }
    for name, html in cases.items():
        out = _enrich(html)
        assert out["publish_ts"] == ts, f"{name} 没认出来：{out}"
        assert out["publish_at"], f"{name} 没给出可读时间"


def test_no_publish_time_means_stay_zero_not_garbage():
    """页面里没有时间标记（验证码页 / 环境异常页）→ 保持 0，不许瞎猜。

    0 是「不知道」的约定值，界面据此显示「未知」；猜一个当前时间会让一篇
    老文章挂着今天的日期，比空着更误导。
    """
    for html in ("", "<html><body>环境异常，请稍后再试</body></html>", None):
        out = _enrich(html or "")
        assert out["publish_ts"] == 0
        assert "publish_at" not in out or out["publish_at"] == ""


def test_title_is_html_unescaped():
    """标题要反转义 —— og:title 里的引号是 &quot;，不处理会原样显示在列表里。

    实测真页面取到的是：``刺破&quot;默认配置&quot;：以 vLLM 为例…``。
    """
    html = '<meta property="og:title" content="刺破&quot;默认配置&quot;：A &amp; B" />'
    out = _enrich(html)
    assert out["title"] == '刺破"默认配置"：A & B', out["title"]


def test_title_prefers_activity_name_over_og_title():
    """正文里的 activity-name 更准（og:title 有时带后缀），有就用它。"""
    html = (
        '<meta property="og:title" content="标题 - 公众号名" />'
        '<h1 id="activity-name"> 真正的标题 </h1>'
    )
    assert _enrich(html)["title"] == "真正的标题"
