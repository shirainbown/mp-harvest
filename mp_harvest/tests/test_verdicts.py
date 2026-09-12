"""「两阶段判定 → 最终判定」这条规则（2026-09）。

用户报的原话：**「其他来源的文章，通过 AI 筛选之后根本体现不出来筛选前后的
区别」**。根因不是筛选没生效 —— 周报那边算出了最终判定、筛选确实生效了 ——
而是**列表端没算**：界面「判定」列读的是最终 ``keep``，而列表只把缓存里的
``title_keep`` 合并进了行里，于是筛完仍然是满屏「待判」。

同一个优先级规则当时有两个实现（周报一份、公众号侧一份），列表端第三处压根
没有 —— 漂移是必然的。现在外部那两个调用方共用 ``core.verdicts.final_verdict``，
这里用**真实现**（不是 server 契约测试里的假模块）钉住规则本身。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core.verdicts import final_verdict  # noqa: E402


def test_content_stage_wins_over_title():
    """内容筛选是第二阶段，它的判定才是最终意见。"""
    keep, reason = final_verdict(
        {"title_keep": True, "title_reason": "标题说留"},
        {"content_keep": False, "content_reason": "正文说扔"},
    )
    assert keep is False
    assert reason == "正文说扔", "理由必须来自内容阶段，不能张冠李戴"


def test_title_stage_used_when_content_not_judged():
    keep, reason = final_verdict(
        {"title_keep": False, "title_reason": "标题说扔"}, None
    )
    assert keep is False and reason == "标题说扔"


def test_content_reason_not_reused_for_title_verdict():
    """内容条目存在但**没有判定**（只有别的字段）时，仍然退回标题阶段。

    判据是 ``content_keep is not None``，不是「内容条目在不在」—— 用「在不在」
    判断的话，一条只写了正文缓存（没有判定）的记录会把标题判定吞掉。
    """
    keep, reason = final_verdict(
        {"title_keep": True, "title_reason": "标题说留"},
        {"body_text": "只有正文，没有判定"},
    )
    assert keep is True and reason == "标题说留"


def test_unjudged_stays_none():
    """两阶段都没判过 → (None, "")；**不能**给个默认值。

    给默认值（比如 False）会让「还没筛」的条目在周报里被当成「已筛掉」，
    用户会觉得系统在偷偷丢他的文章；反过来给 True 则等于筛选没生效。
    """
    for title, content in ((None, None), ({}, {}), ({"title_reason": "只有理由"}, {})):
        keep, reason = final_verdict(title, content)
        assert keep is None, (title, content)
        assert reason == ""


def test_verdict_is_a_real_boolean():
    """返回的必须是 bool，不能是 0/1/字符串 —— 下游到处是 ``is True`` 比较。"""
    for entry in ({"title_keep": True}, {"title_keep": 1}, {"title_keep": "yes"}):
        keep, _ = final_verdict(entry, None)
        assert keep is True, entry
