"""自定义日期范围拉取（fetch_history_range）与 sightings end_ts 过滤测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core import history_client  # noqa: E402
from mp_harvest.core.history_client import (  # noqa: E402
    fetch_history_days,
    fetch_history_range,
    merge_articles_with_sightings,
)

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
        _mk_page([_mk_art(1, TS["d20"]), _mk_art(2, TS["d15"]), _mk_art(3, TS["d10"])]),
        _mk_page([_mk_art(4, TS["d05"]), _mk_art(5, TS["d01"])]),
    ]
    saved = _patch_pages(pages)
    try:
        res = fetch_history_range(
            CRED, start_ts=TS["d05"], end_ts=TS["d15"], sleep_s=0
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
        res = fetch_history_range(CRED, start_ts=TS["d05"], sleep_s=0)
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
        res = fetch_history_range(CRED, start_ts=TS["d05"], end_ts=TS["d15"], sleep_s=0)
    finally:
        _restore(saved)
    # 第 2 页 next_offset=None → 按 count 续翻一次，拿到空页才停
    assert res["pages"] == 3
    assert [a["publish_ts"] for a in res["articles"]] == [TS["d10"]]


def test_range_end_ts_before_start_clamped():
    """end_ts < start_ts 时收敛为 start_ts（不报错、不收录窗口外文章）。"""
    pages = [_mk_page([_mk_art(1, TS["d10"])])]
    saved = _patch_pages(pages)
    try:
        res = fetch_history_range(CRED, start_ts=TS["d10"], end_ts=TS["d05"], sleep_s=0)
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
        res = fetch_history_days(CRED, days=7, sleep_s=0)
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
            sleep_s=0,
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
            ],
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
        res = fetch_history_range(CRED, start_ts=TS["d01"], max_pages=2, sleep_s=0)
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
        res = fetch_history_range(CRED, start_ts=TS["d01"], max_pages=100, sleep_s=0)
    finally:
        _restore(saved)

    assert res["truncated"] is False, res
    assert res["warning"] == ""
