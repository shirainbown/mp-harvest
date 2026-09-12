"""自定义日期范围拉取（fetch_history_range）与 sightings end_ts 过滤测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core import history_client  # noqa: E402
from mp_harvest.core.history_client import (  # noqa: E402
    NO_DELAY,
    fetch_history_days,
    fetch_history_range,
    merge_articles_with_sightings,
)


def policy(**over):
    """测试用策略：不等待、不重试；按需覆盖 max_pages 之类。

    真等 3–8 秒的话翻页用例要跑几分钟 —— 默认策略是给真实网络用的。
    """
    import dataclasses

    return dataclasses.replace(NO_DELAY, **over)


CRED = {"__biz": "B", "uin": "u", "key": "k"}

# 固定时间锚点：2026-08-01 ~ 2026-08-20 之间的若干发布时间
TS = {
    "d01": 1785518400,  # 2026-08-01 12:00 (UTC+8)
    "d05": 1785864000,  # 2026-08-05 12:00
    "d10": 1786296000,  # 2026-08-10 12:00
    "d15": 1786728000,  # 2026-08-15 12:00
    "d20": 1787160000,  # 2026-08-20 12:00
}


def _mk_page(articles, *, can_continue=False, next_offset=None):
    """构造 fetch_getmsg_page 的返回值（raw.general_msg_list 与 articles 等长）。"""
    return {
        "ok": True,
        "articles": articles,
        "can_continue": can_continue,
        "next_offset": next_offset,
        "nickname": "测试号",
        "raw": {"general_msg_list": json.dumps({"list": [{} for _ in articles]})},
    }


def _mk_art(i, ts):
    return {
        "title": f"文章{i}",
        "link": f"https://mp.weixin.qq.com/s?__biz=B&mid={i}&idx=1&sn=s{i}",
        "publish_ts": ts,
        "identity": f"mid:{i}|idx:1|sn:s{i}",
    }


def _patch_pages(monkey_pages):
    """monkey_pages: list of page dicts，按调用顺序返回。"""
    it = iter(monkey_pages)
    orig_page = history_client.fetch_getmsg_page
    orig_nick = history_client.fetch_profile_nickname
    history_client.fetch_getmsg_page = lambda cred, *, offset=0, count=10, session=None: next(
        it, _mk_page([])
    )
    history_client.fetch_profile_nickname = lambda cred, session=None: "测试号"
    return orig_page, orig_nick


def _restore(saved):
    history_client.fetch_getmsg_page = saved[0]
    history_client.fetch_profile_nickname = saved[1]


def test_range_keeps_only_window_articles():
    """start/end 闭区间：早于 start、晚于 end 的都不收录。"""
    pages = [
        _mk_page([_mk_art(1, TS["d20"]), _mk_art(2, TS["d15"]), _mk_art(3, TS["d10"])], can_continue=True, next_offset=10),
        _mk_page([_mk_art(4, TS["d05"]), _mk_art(5, TS["d01"])]),
    ]
    saved = _patch_pages(pages)
    try:
        res = fetch_history_range(
            CRED, start_ts=TS["d05"], end_ts=TS["d15"], policy=policy()
        )
    finally:
        _restore(saved)
    assert res["ok"]
    ts_list = sorted(a["publish_ts"] for a in res["articles"])
    assert ts_list == [TS["d05"], TS["d10"], TS["d15"]]
    assert res["start_ts"] == TS["d05"]
    assert res["end_ts"] == TS["d15"]


def test_range_stops_when_page_fully_old():
    """整页最新 ts 都老于 start_ts 即停止翻页。"""
    pages = [
        _mk_page([_mk_art(1, TS["d10"])], can_continue=True, next_offset=10),
        _mk_page([_mk_art(2, TS["d01"])], can_continue=True, next_offset=20),
        _mk_page([_mk_art(3, TS["d20"])]),  # 不应被请求到
    ]
    saved = _patch_pages(pages)
    try:
        res = fetch_history_range(CRED, start_ts=TS["d05"], policy=policy())
    finally:
        _restore(saved)
    assert res["ok"]
    assert res["pages"] == 2
    assert [a["publish_ts"] for a in res["articles"]] == [TS["d10"]]


def test_range_newer_than_end_skipped_but_keeps_paging():
    """比 end_ts 新的文章跳过但继续翻页，直到进入窗口。"""
    pages = [
        _mk_page([_mk_art(1, TS["d20"])], can_continue=True, next_offset=10),
        _mk_page([_mk_art(2, TS["d10"])]),
    ]
    saved = _patch_pages(pages)
    try:
        res = fetch_history_range(CRED, start_ts=TS["d05"], end_ts=TS["d15"], policy=policy())
    finally:
        _restore(saved)
    # 第 2 页 can_continue=False（微信说没有下一页）→ 停在这里。
    # 2026-09 之前会「按 count 续翻一次、拿到空页才停」—— 那个多余请求正是
    # 社区实测里触发账号级风控（ret=-6）的路径，已去掉。
    assert res["pages"] == 2
    assert [a["publish_ts"] for a in res["articles"]] == [TS["d10"]]


def test_range_end_ts_before_start_clamped():
    """end_ts < start_ts 时收敛为 start_ts（不报错、不收录窗口外文章）。"""
    pages = [_mk_page([_mk_art(1, TS["d10"])])]
    saved = _patch_pages(pages)
    try:
        res = fetch_history_range(CRED, start_ts=TS["d10"], end_ts=TS["d05"], policy=policy())
    finally:
        _restore(saved)
    assert res["end_ts"] == TS["d10"]
    assert [a["publish_ts"] for a in res["articles"]] == [TS["d10"]]


def test_fetch_history_days_delegates_to_range():
    """fetch_history_days 薄封装：cutoff = now - days*86400，返回字段保持兼容。"""
    import time as _time

    pages = [_mk_page([_mk_art(1, int(_time.time()))])]
    saved = _patch_pages(pages)
    try:
        res = fetch_history_days(CRED, days=7, policy=policy())
    finally:
        _restore(saved)
    assert res["ok"]
    assert res["days"] == 7
    assert res["cutoff_ts"] == res["start_ts"]
    assert abs(res["cutoff_ts"] - (int(_time.time()) - 7 * 86400)) < 60
    assert len(res["articles"]) == 1


def test_merge_sightings_respects_end_ts():
    """sightings 合并：晚于 end_ts 的目击不并入。"""
    base = [_mk_art(1, TS["d10"])]
    sightings = [
        {"title": "窗口内", "link": "https://mp.weixin.qq.com/s?__biz=B&mid=2&idx=1&sn=a",
         "publish_ts": TS["d15"]},
        {"title": "太晚", "link": "https://mp.weixin.qq.com/s?__biz=B&mid=3&idx=1&sn=b",
         "publish_ts": TS["d20"]},
        {"title": "太早", "link": "https://mp.weixin.qq.com/s?__biz=B&mid=4&idx=1&sn=c",
         "publish_ts": TS["d01"]},
    ]
    merged = merge_articles_with_sightings(
        base, sightings, cutoff_ts=TS["d05"], end_ts=TS["d15"]
    )
    titles = {a["title"] for a in merged}
    assert titles == {"文章1", "窗口内"}


def test_merged_sightings_is_notice_not_truncation():
    """合并补录/抓包是 notice（好消息），不能和 truncated（可能没拉完）混为一谈。

    2026-09 实测撞到的回归：两者原先共用 ``warning`` 字段，前端在**成功分支**
    也显示 warning（本意是补上「翻页上限截断」的提示），结果一次完全成功的拉取
    被渲染成红色错误「拉取未完整：已合并补录/抓包 1 篇」。
    """
    ts = TS["d05"]
    saved = _patch_pages([_mk_page([_mk_art(1, ts)])])
    try:
        res = fetch_history_range(
            CRED,
            start_ts=TS["d01"],
            sightings=[
                {
                    # getmsg 不会返回这一篇（mid=99），只能靠目击补上
                    "title": "只有浏览时才看到的文章",
                    "link": "https://mp.weixin.qq.com/s?__biz=B&mid=99&idx=1&sn=s99",
                    "identity": "mid:99|idx:1|sn:s99",
                    "publish_ts": ts,
                    "source": "sighting",
                    "__biz": "B",
                }
            ], policy=policy()
        )
    finally:
        _restore(saved)

    assert res["ok"] is True
    assert "已合并" in res["notice"], res
    assert res["truncated"] is False, res
    # warning 仍保留合并文本（向后兼容），但调用方应改用 truncated / notice
    assert "已合并" in res["warning"]


def test_page_cap_sets_truncated_flag():
    """翻页上限命中时 truncated=True（这才是需要提醒「可能没拉完」的信号）。"""
    # next_offset 必须递增，否则循环会走 `nxt_i <= offset` 提前 break，测不到上限
    pages = [
        _mk_page([_mk_art(i, TS["d05"])], can_continue=True, next_offset=10 * (i + 1))
        for i in range(1, 4)
    ]
    saved = _patch_pages(pages)
    try:
        res = fetch_history_range(CRED, start_ts=TS["d01"], policy=policy(max_pages=2))
    finally:
        _restore(saved)

    assert res["truncated"] is True, res
    assert "翻页上限" in res["warning"]


def test_normal_completion_is_not_truncated():
    """正常翻完（循环 break 退出）时 truncated 必须为 False，不能误报「没拉完」。

    用户实测那次 2 页就 break 了 —— 如果这里误报，前端又会弹红。
    """
    saved = _patch_pages([_mk_page([_mk_art(1, TS["d05"])], can_continue=False)])
    try:
        res = fetch_history_range(CRED, start_ts=TS["d01"], policy=policy(max_pages=100))
    finally:
        _restore(saved)

    assert res["truncated"] is False, res
    assert res["warning"] == ""


# ── 断点拉取：整页已入库则停止继续请求（2026-09）────────────────────


def _patch_pages_counting(pages):
    """同 _patch_pages，但记录实际请求了几页（断点拉取的核心断言）。"""
    it = iter(pages)
    calls = {"n": 0}
    orig_page = history_client.fetch_getmsg_page
    orig_nick = history_client.fetch_profile_nickname

    def _fake(cred, *, offset=0, count=10, session=None):
        calls["n"] += 1
        return next(it, _mk_page([]))

    history_client.fetch_getmsg_page = _fake
    history_client.fetch_profile_nickname = lambda cred, session=None: "测试号"
    return (orig_page, orig_nick), calls


def test_stops_paging_when_page_fully_known():
    """第二页全是老熟人 → 不再请求第三页。

    不这么做的话，「近 90 天」每次拉取都会把几十页从头重新请求一遍，
    全打在微信上 —— 这正是封控风险的主要来源。
    """
    pages = [
        _mk_page([_mk_art(9, TS["d20"])], can_continue=True, next_offset=10),                       # 新文章，未知
        _mk_page([_mk_art(2, TS["d15"]), _mk_art(3, TS["d10"])], can_continue=True, next_offset=20),  # 全已知
        _mk_page([_mk_art(4, TS["d05"])]),                       # 不该被请求
    ]
    saved, calls = _patch_pages_counting(pages)
    try:
        res = fetch_history_range(
            CRED,
            start_ts=TS["d01"],
            known_keys={"mid:2|idx:1|sn:s2", "mid:3|idx:1|sn:s3"}, policy=policy()
        )
    finally:
        _restore(saved)

    assert res["ok"] is True
    assert calls["n"] == 2, f"应只请求 2 页，实际 {calls['n']}"
    assert res["stopped_early"] is True
    # 停下的那一页本身仍要被收录（内容已经拿到，不发多余请求）
    assert {a["title"] for a in res["articles"]} == {"文章9", "文章2", "文章3"}


def test_keeps_paging_while_page_has_unknown():
    """页面里还有没见过的文章就继续翻 —— 不能提前停掉漏数据。"""
    pages = [
        _mk_page([_mk_art(9, TS["d20"])], can_continue=True, next_offset=10),
        _mk_page([_mk_art(8, TS["d15"])], can_continue=True, next_offset=20),  # 未知
        # 这一页全已知、**且微信说后面还有**（can_continue=True）—— 两个条件缺一不可：
        # 若这里写 False，会先被「微信说没有下一页」那条挡掉，走不到 known-keys
        # 分支，用例就测了个寂寞
        _mk_page([_mk_art(2, TS["d10"])], can_continue=True, next_offset=30),  # 已知
    ]
    saved, calls = _patch_pages_counting(pages)
    try:
        res = fetch_history_range(
            CRED, start_ts=TS["d01"], known_keys={"mid:2|idx:1|sn:s2"}, policy=policy()
        )
    finally:
        _restore(saved)

    assert calls["n"] == 3
    assert res["stopped_early"] is True  # 第 3 页触发停止


def test_stop_ignores_out_of_window_articles():
    """比 end_ts 新的文章本轮本就要跳过，不能因为它们「未知」就一路翻下去。

    **同页混合**才是判别用例：窗口外未知 + 窗口内已知。真实场景是账号今天又推了
    新文章，而用户拉的是上周的区间 —— 翻到「新文章 + 上周已知文章」混排的那一页时
    就该停。把窗口外的文章单独放一页是测不出问题的：那种页面两边都会继续翻。
    """
    pages = [
        _mk_page([_mk_art(99, TS["d20"]), _mk_art(2, TS["d10"])], can_continue=True, next_offset=10),  # 同页：窗外未知 + 窗内已知
        _mk_page([_mk_art(3, TS["d05"])]),                          # 不该被请求
    ]
    saved, calls = _patch_pages_counting(pages)
    try:
        res = fetch_history_range(
            CRED,
            start_ts=TS["d01"],
            end_ts=TS["d15"],
            known_keys={"mid:2|idx:1|sn:s2"}, policy=policy()
        )
    finally:
        _restore(saved)

    assert calls["n"] == 1, f"窗口外文章不该阻止提前停止，实际请求 {calls['n']} 页"
    assert res["stopped_early"] is True
    assert {a["title"] for a in res["articles"]} == {"文章2"}  # 窗口外的 99 不收录


def test_page_with_no_in_window_article_does_not_stop():
    """整页都在窗口外 → 无从判断是否到过断点，必须继续翻。"""
    pages = [
        _mk_page([_mk_art(99, TS["d20"])], can_continue=True, next_offset=10),  # 全在窗口外（晚于 end_ts）
        _mk_page([_mk_art(2, TS["d10"])], can_continue=True, next_offset=20),   # 窗口内、已知 → 在这里停
        _mk_page([_mk_art(3, TS["d05"])]),
    ]
    saved, calls = _patch_pages_counting(pages)
    try:
        res = fetch_history_range(
            CRED,
            start_ts=TS["d01"],
            end_ts=TS["d15"],
            known_keys={"mid:2|idx:1|sn:s2"}, policy=policy()
        )
    finally:
        _restore(saved)

    assert calls["n"] == 2, "全窗口外的页面不能触发停止"
    assert res["stopped_early"] is True


def test_without_known_keys_behaves_as_before():
    """不传 known_keys（老调用方 / 首次拉取）→ 行为与改造前一致，绝不提前停。"""
    pages = [
        _mk_page([_mk_art(1, TS["d05"])], can_continue=True, next_offset=10),
        _mk_page([_mk_art(2, TS["d10"])], can_continue=True, next_offset=20),
        _mk_page([_mk_art(3, TS["d15"])]),
    ]
    saved, calls = _patch_pages_counting(pages)
    try:
        res = fetch_history_range(CRED, start_ts=TS["d01"], policy=policy())
    finally:
        _restore(saved)

    # 3 页翻完（第 3 页 can_continue=False）就停，不再多打一个必然为空的请求
    assert calls["n"] == 3, "没有已知集合时应一路翻到微信说没有下一页"
    assert res["stopped_early"] is False


def test_stopped_early_false_on_normal_completion():
    """翻到「整页都老于 start_ts」自然结束 → 不算断点命中，别让前端误报。"""
    pages = [
        _mk_page([_mk_art(1, TS["d20"])], can_continue=True, next_offset=10),
        _mk_page([_mk_art(2, TS["d01"])]),
    ]
    saved, calls = _patch_pages_counting(pages)
    try:
        res = fetch_history_range(
            CRED, start_ts=TS["d10"], known_keys={"mid:2|idx:1|sn:s2"}, policy=policy()
        )
    finally:
        _restore(saved)

    assert res["stopped_early"] is False


# ── 限流与节奏（2026-09）───────────────────────────────────────────
#
# 微信没有公开的限流文档，下面几条钉的是「按社区实测该怎么反应」：
# 及时停、别重试、给能指导行动的话。


def _patch_raw(handler):
    """把 fetch_getmsg_page 换成一个自定义函数；返回还原用的句柄。"""
    orig_page = history_client.fetch_getmsg_page
    orig_nick = history_client.fetch_profile_nickname
    history_client.fetch_getmsg_page = handler
    history_client.fetch_profile_nickname = lambda cred, session=None: "测试号"
    return orig_page, orig_nick


def test_can_continue_false_stops_without_extra_request():
    """微信说没有下一页就**立刻停**，不再多打一个必然为空的请求。

    这次改动的核心：那个多余请求正是社区实测里触发**账号级**风控（ret=-6，
    之后该微信号抓任何公众号都失败、恢复约 24 小时）的路径。
    """
    calls = {"n": 0}
    pages = [
        _mk_page([_mk_art(1, TS["d10"])], can_continue=True, next_offset=10),
        _mk_page([_mk_art(2, TS["d05"])]),          # can_continue=False → 停
        _mk_page([_mk_art(3, TS["d01"])]),          # 不该被请求
    ]
    it = iter(pages)

    def handler(cred, *, offset=0, count=10, session=None):
        calls["n"] += 1
        return next(it, _mk_page([]))

    saved = _patch_raw(handler)
    try:
        res = fetch_history_range(CRED, start_ts=TS["d01"], policy=policy())
    finally:
        _restore(saved)

    assert calls["n"] == 2, f"应只请求 2 页，实际 {calls['n']}"
    assert res["ok"] is True


def test_rate_limit_message_is_actionable_and_not_retried():
    """被限流时：给一句能指导行动的话，而且**绝不自动重试**。

    重试是在被封锁时继续敲门 —— 社区实测那么做会把封锁拖得更久。
    所以这里同时钉两件事：请求只发一次、错误文案里提到「等」和「换号」。
    """
    calls = {"n": 0}

    def handler(cred, *, offset=0, count=10, session=None):
        calls["n"] += 1
        page = _mk_page([])
        page.update({
            "ok": False, "rate_limited": True,
            "error": history_client.RATE_LIMIT_MESSAGE,
        })
        return page

    saved = _patch_raw(handler)
    try:
        res = fetch_history_range(
            CRED, start_ts=TS["d01"], policy=policy(retries=3),
        )
    finally:
        _restore(saved)

    assert res["ok"] is False
    assert res["rate_limited"] is True
    assert calls["n"] == 1, f"限流不该重试，实际请求 {calls['n']} 次"
    assert "24 小时" in res["error"] and "换一个微信号" in res["error"]


def test_is_rate_limited_recognises_the_known_signals():
    """限流的识别：返回码与文案两条路都要认。"""
    f = history_client.is_rate_limited
    assert f(-6, "unknownerror") is True          # 账号级风控
    assert f("200013", "") is True                # 另一种频控
    assert f(-3, "") is True                      # 频率限制
    assert f(0, "ok") is False                    # 正常
    assert f(-1, "系统错误") is False             # 别的错误不当限流
    assert f(0, "操作频繁") is True               # 只有文案时也要认出来


def test_network_error_is_retried_with_backoff():
    """网络类错误（超时/连接断）**值得**重试 —— 那是偶发，不是被限流。"""
    calls = {"n": 0}

    def handler(cred, *, offset=0, count=10, session=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("连接被重置")
        return _mk_page([_mk_art(1, TS["d10"])])

    saved = _patch_raw(handler)
    try:
        # retries=2 → 最多 3 次尝试；前两次抛、第三次成
        res = fetch_history_range(CRED, start_ts=TS["d01"], policy=policy(retries=2))
    finally:
        _restore(saved)

    assert calls["n"] == 3
    assert res["ok"] is True and len(res["articles"]) == 1


def test_network_error_gives_up_after_retries():
    """重试用完还不行 → 如实报错，不要无限重试。"""
    calls = {"n": 0}

    def handler(cred, *, offset=0, count=10, session=None):
        calls["n"] += 1
        raise ConnectionError("一直连不上")

    saved = _patch_raw(handler)
    try:
        res = fetch_history_range(CRED, start_ts=TS["d01"], policy=policy(retries=1))
    finally:
        _restore(saved)

    assert calls["n"] == 2, f"retries=1 应尝试 2 次，实际 {calls['n']}"
    assert res["ok"] is False and "连不上" in res["error"]


def test_policy_delay_is_randomised_within_the_range():
    """页间延迟取随机值：固定间隔本身就是一个可识别的特征。"""
    p = history_client.FetchPolicy(delay_min=3, delay_max=8)
    got = [p.delay() for _ in range(50)]
    assert all(3.0 <= v <= 8.0 for v in got), got
    assert len(set(got)) > 1, "一直是同一个值 = 没随机"
    # 上下界写反也要能用（设置页填反了不该崩）
    assert history_client.FetchPolicy(delay_min=9, delay_max=2).delay() <= 9.0


def test_cooldown_every_n_pages(monkeypatch):
    """每翻 N 页多歇一次：匀速翻到底比「翻一会儿歇一会儿」更像机器。"""
    sleeps = []
    monkeypatch.setattr(history_client.time, "sleep", lambda s: sleeps.append(s))
    # 第 5 页 can_continue=False（微信说没有下一页）→ 停；它自己不会再歇
    pages = [
        _mk_page([_mk_art(i, TS["d05"])], can_continue=(i < 5), next_offset=10 * (i + 1))
        for i in range(1, 6)
    ]
    saved = _patch_pages(pages)
    try:
        res = fetch_history_range(
            CRED, start_ts=TS["d01"],
            policy=policy(cooldown_every=2, cooldown_seconds=60),
        )
    finally:
        _restore(saved)

    assert res["ok"] and res["pages"] == 5
    # 只在第 2、4 页之后各歇一次（delay 为 0 的那些不计）
    assert [s for s in sleeps if s > 0] == [60.0, 60.0], sleeps


def test_cooldown_can_be_switched_off(monkeypatch):
    """cooldown_pages=0 = 不额外歇（用户嫌慢时可以关掉）。"""
    sleeps = []
    monkeypatch.setattr(history_client.time, "sleep", lambda s: sleeps.append(s))
    pages = [
        _mk_page([_mk_art(i, TS["d05"])], can_continue=(i < 5), next_offset=10 * (i + 1))
        for i in range(1, 6)
    ]
    saved = _patch_pages(pages)
    try:
        res = fetch_history_range(
            CRED, start_ts=TS["d01"],
            policy=policy(cooldown_every=0, cooldown_seconds=60),
        )
    finally:
        _restore(saved)

    assert res["ok"] and res["pages"] == 5
    assert [s for s in sleeps if s > 0] == [], sleeps
