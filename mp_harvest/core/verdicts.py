"""两阶段 AI 判定 → 「最终判定」的那条规则。

**为什么单独一个模块**：这条规则有两个调用方（外部条目列表、周报候选），
而它们各写一遍已经漂移过一次 —— 2026-09 用户报的「其他来源筛完看不出区别」，
根因就是周报那份算出了最终判定、列表那份没算，界面上「判定」列永远是「待判」。

放在 ``core/ai_filter`` 里本该最自然，但 server 契约测试会把那个模块换成假模块
（见 ``tests/server/conftest.py`` 的 FAKE_MODULES），而**这条规则正是被测对象**
——放进假模块里，测试就变成测那个替身，等于没测。所以单独放一个不被替换的模块，
两个调用方共用同一份实现。

公众号侧还有一份等价规则（``server.state.merge_article_verdicts``），那边是按
**行上的字段**算（``row["content_keep"]``），这里按**缓存条目**算
（``entry["content_keep"]``）—— 语义相同、输入形态不同，暂时各自保留，
改动其一时请对照另一处。
"""

from __future__ import annotations

from typing import Any

__all__ = ["final_verdict"]


def final_verdict(
    title_entry: dict[str, Any] | None,
    content_entry: dict[str, Any] | None,
) -> tuple[bool | None, str]:
    """两阶段缓存条目 → ``(最终判定, 理由)``。

    - **内容筛选优先**：它是第二阶段，才是「留不留」的最终意见；
    - 没有内容判定时才用标题判定；
    - 两阶段都没判过 → ``(None, "")``（未判定，界面显示「待判」）；
    - 理由跟着**它自己那一阶段**走 —— 一律取某一个会给出另一阶段的理由。
    """
    t = title_entry or {}
    c = content_entry or {}
    ck = c.get("content_keep")
    tk = t.get("title_keep")
    keep = ck if ck is not None else tk
    if keep is None:
        return None, ""
    reason = (
        c.get("content_reason") if ck is not None else t.get("title_reason")
    ) or ""
    return bool(keep), str(reason)
