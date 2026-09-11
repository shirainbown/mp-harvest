"""WeChat MP history list via profile_ext?action=getmsg (same as MP Harvest).

Requires short-lived credentials: __biz, uin, key, pass_ticket (recommended).
"""

from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

import requests

GETMSG_URL = "https://mp.weixin.qq.com/mp/profile_ext"
REQUIRED_CRED_KEYS = ("__biz", "uin", "key")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "WindowsWechat(0x63090a13) XWEB/11275"
)

# getmsg ``count`` is number of *push messages* (推送), not articles.
# Busy accounts may push many times/day; keep paging until date cutoff.
DEFAULT_PAGE_COUNT = 10
DEFAULT_MAX_PAGES = 100


def _fully_unquote(value: str) -> str:
    s = value or ""
    for _ in range(3):
        n = unquote(s)
        if n == s:
            break
        s = n
    return s


def normalize_credentials(cred: dict[str, Any]) -> dict[str, Any]:
    out = dict(cred)
    for k in ("__biz", "uin", "key", "pass_ticket", "appmsg_token", "wxtoken"):
        if k in out and isinstance(out[k], str):
            out[k] = _fully_unquote(out[k].strip())
    return out


def validate_credentials(cred: dict[str, Any]) -> tuple[bool, str]:
    missing = [k for k in REQUIRED_CRED_KEYS if not str(cred.get(k) or "").strip()]
    if missing:
        return False, f"缺少字段: {', '.join(missing)}"
    return True, ""


def _clean_url(raw: str) -> str:
    s = html.unescape((raw or "").strip())
    s = s.replace("\\/", "/")
    s = s.replace("\\u0026", "&").replace("\\x26", "&")
    s = s.replace("&amp;", "&")
    if s.startswith("//"):
        s = "https:" + s
    if s.startswith("http://mp.weixin.qq.com"):
        s = "https://" + s[len("http://") :]
    return s


def _link_from_item(item: dict[str, Any]) -> str:
    for key in (
        "content_url",
        "content_url_encoded",
        "url",
        "link",
        "content_url_with_token",
    ):
        raw = item.get(key)
        if isinstance(raw, str) and raw.strip():
            return _clean_url(raw)
    return ""


def _mid_idx_sn(link: str) -> tuple[str, str, str]:
    link = _clean_url(link)
    if not link:
        return "", "", ""
    try:
        q = parse_qs(urlparse(link).query)
    except Exception:
        return "", "", ""
    mid = (q.get("mid") or q.get("appmsgid") or [""])[0]
    idx = (q.get("idx") or q.get("itemidx") or [""])[0]
    sn = (q.get("sn") or [""])[0]
    return str(mid or ""), str(idx or ""), str(sn or "")


def article_identity(
    link: str,
    *,
    title: str = "",
    publish_ts: int = 0,
    msg_id: str = "",
    ordinal: int = 0,
) -> str:
    """Stable identity for dedupe.

    Prefer mid+idx (same push, different articles). Never collapse two articles
    that only share __biz / bare /s path.
    """
    link = _clean_url(link)
    mid, idx, sn = _mid_idx_sn(link)
    if mid:
        # idx distinguishes 同一条多图文里的第 1/2/… 篇
        return f"mid:{mid}|idx:{idx or str(ordinal or 1)}|sn:{sn}"

    if link:
        try:
            u = urlparse(link)
        except Exception:
            u = None
        if u is not None:
            m = re.search(r"/s/([A-Za-z0-9_-]+)", u.path or "")
            if m:
                return f"s:{m.group(1)}"
            # Keep full URL path+query (minus fragment) — do not over-strip
            return urlunparse((u.scheme, u.netloc, u.path, "", u.query, ""))

    # Last resort: never merge different titles from the same push
    return f"msg:{msg_id}|ord:{ordinal}|ts:{publish_ts}|t:{title}"


def _item_to_row(
    item: dict[str, Any],
    publish_ts: int,
    *,
    msg_id: str = "",
    ordinal: int = 0,
) -> dict[str, Any] | None:
    title = (item.get("title") or "").strip()
    link = _link_from_item(item)
    # 头条偶发缺 link：只要有标题也保留，避免「同一天多图文只剩最后一篇」
    if not link and not title:
        return None
    if not title:
        title = "(无标题)"
    mid, idx, sn = _mid_idx_sn(link)
    return {
        "title": title,
        "link": link,
        "digest": (item.get("digest") or "").strip(),
        "cover": _clean_url(item.get("cover") or item.get("cdn_url") or ""),
        "author": (item.get("author") or "").strip(),
        "publish_ts": publish_ts,
        "publish_at": (
            datetime.fromtimestamp(publish_ts).strftime("%Y-%m-%d %H:%M")
            if publish_ts
            else ""
        ),
        "mid": mid,
        "idx": idx or str(ordinal or 1),
        "sn": sn,
        "identity": article_identity(
            link,
            title=title,
            publish_ts=publish_ts,
            msg_id=msg_id,
            ordinal=ordinal,
        ),
    }


def _iter_msg_article_items(msg: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """Yield (ordinal, item) for head + multi-app articles in one push.

    WeChat layout:
      - 第 1 篇在 app_msg_ext_info
      - 第 2..N 篇在 app_msg_ext_info.multi_app_msg_item_list
    Some payloads also put multi on the message root — include those too.
    """
    app = msg.get("app_msg_ext_info") or {}
    if not isinstance(app, dict):
        app = {}

    items: list[tuple[int, dict[str, Any]]] = []
    if app:
        # ordinal 1 = 头条
        items.append((1, app))

    multi = app.get("multi_app_msg_item_list")
    if not isinstance(multi, list):
        multi = []
    # Fallback: multi list wrongly nested at message root (seen in some scrapers)
    root_multi = msg.get("multi_app_msg_item_list")
    if isinstance(root_multi, list) and root_multi:
        multi = list(multi) + [x for x in root_multi if x not in multi]

    for i, sub in enumerate(multi):
        if isinstance(sub, dict):
            items.append((i + 2, sub))
    return items


def parse_general_msg_list(payload: dict[str, Any] | str) -> list[dict[str, Any]]:
    if isinstance(payload, str):
        payload = json.loads(payload) if payload.strip() else {}
    rows: list[dict[str, Any]] = []
    for msg in payload.get("list") or []:
        if not isinstance(msg, dict):
            continue
        comm = msg.get("comm_msg_info") or {}
        if not isinstance(comm, dict):
            comm = {}
        # type 49 = 图文；缺省时只要有 app_msg_ext_info 也尝试解析
        msg_type = comm.get("type")
        if msg_type is not None and str(msg_type) not in ("49", "49.0"):
            # 仍可能有图文扩展，不直接跳过
            pass
        publish_ts = int(comm.get("datetime") or 0)
        msg_id = str(comm.get("id") or "")

        app = msg.get("app_msg_ext_info")
        if not isinstance(app, dict) or not app:
            continue

        for ordinal, item in _iter_msg_article_items(msg):
            row = _item_to_row(
                item,
                publish_ts,
                msg_id=msg_id,
                ordinal=ordinal,
            )
            if row:
                rows.append(row)
    return rows


def parse_getmsg_response(payload: dict[str, Any]) -> dict[str, Any]:
    ret = payload.get("ret")
    errmsg = str(payload.get("errmsg") or "")
    if ret not in (0, "0") and errmsg != "ok":
        return {
            "ok": False,
            "error": errmsg or f"ret={ret}",
            "articles": [],
            "can_continue": False,
            "next_offset": None,
            "raw": payload,
        }
    gml = payload.get("general_msg_list") or ""
    if isinstance(gml, dict):
        articles = parse_general_msg_list(gml)
    else:
        articles = parse_general_msg_list(str(gml))
    can = payload.get("can_msg_continue")
    return {
        "ok": True,
        "error": "",
        "articles": articles,
        "can_continue": bool(int(can)) if can is not None and str(can).isdigit() else bool(can),
        "next_offset": payload.get("next_offset"),
        "msg_count": payload.get("msg_count"),
        "nickname": str(payload.get("nickname") or "").strip(),
        "raw": payload,
    }


def fetch_getmsg_page(
    cred: dict[str, Any],
    *,
    offset: int = 0,
    count: int = DEFAULT_PAGE_COUNT,
    timeout: float = 25.0,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    cred = normalize_credentials(cred)
    ok, err = validate_credentials(cred)
    if not ok:
        return {
            "ok": False,
            "error": err,
            "articles": [],
            "can_continue": False,
            "next_offset": None,
        }

    params = {
        "action": "getmsg",
        "__biz": str(cred["__biz"]).strip(),
        "f": "json",
        "offset": str(offset),
        "count": str(count),
        "is_ok": "1",
        "scene": "124",
        "uin": str(cred["uin"]).strip(),
        "key": str(cred["key"]).strip(),
        "wxtoken": str(cred.get("wxtoken") or ""),
        "devicetype": str(cred.get("devicetype") or ""),
        "clientversion": str(cred.get("clientversion") or "0"),
        "x5": "0",
    }
    if cred.get("pass_ticket"):
        params["pass_ticket"] = str(cred["pass_ticket"]).strip()
    if cred.get("appmsg_token"):
        params["appmsg_token"] = str(cred["appmsg_token"]).strip()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 "
            "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
            "WindowsWechat(0x63090a13) XWEB/11275"
        ),
        "Referer": (
            f"https://mp.weixin.qq.com/mp/profile_ext?action=home"
            f"&__biz={params['__biz']}&scene=124#wechat_redirect"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    cookies: dict[str, str] = {}
    if cred.get("pass_ticket"):
        cookies["pass_ticket"] = str(cred["pass_ticket"]).strip()
    if cred.get("uin"):
        cookies["wxuin"] = str(cred["uin"]).strip()

    sess = session or requests.Session()
    # Bypass system MITM proxy — talk to WeChat directly.
    sess.trust_env = False
    resp = sess.get(
        GETMSG_URL, params=params, headers=headers, cookies=cookies, timeout=timeout
    )
    text = resp.text.strip()
    if text.startswith("{"):
        payload = resp.json()
    else:
        try:
            payload = json.loads(text)
        except Exception:
            return {
                "ok": False,
                "error": f"非 JSON 响应 status={resp.status_code} body[:160]={text[:160]!r}",
                "articles": [],
                "can_continue": False,
                "next_offset": None,
            }
    page = parse_getmsg_response(payload)
    page["http_status"] = resp.status_code
    page["request_url"] = f"{GETMSG_URL}?{urlencode(params)}"
    return page


def _parse_profile_nickname(html_text: str) -> str:
    """从公众号主页 HTML 里解析官方昵称（2026-08-09：getmsg 不带 nickname）。"""
    patterns = (
        r'"nickname"\s*:\s*"([^"]+)"',
        r"nickname\s*[:=]\s*['\"]([^'\"]+)['\"]",
        r"var\s+nickname\s*=\s*['\"]([^'\"]+)['\"]",
    )
    best = None
    for pattern in patterns:
        m = re.search(pattern, html_text or "", re.I | re.S)
        if m and (best is None or m.start() < best.start()):
            best = m
    if best:
        name = html.unescape(best.group(1)).strip()
        if name:
            return name
    return ""


def fetch_profile_nickname(
    cred: dict[str, Any],
    *,
    timeout: float = 10.0,
    session: requests.Session | None = None,
) -> str:
    """拉公众号主页（action=home）解析官方昵称；失败返回空串（best-effort）。"""
    try:
        cred = normalize_credentials(cred)
        if not validate_credentials(cred)[0]:
            return ""
        params = {
            "action": "home",
            "__biz": str(cred["__biz"]).strip(),
            "f": "json",
            "scene": "124",
            "uin": str(cred["uin"]).strip(),
            "key": str(cred["key"]).strip(),
        }
        if cred.get("pass_ticket"):
            params["pass_ticket"] = str(cred["pass_ticket"]).strip()
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://mp.weixin.qq.com/",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        }
        cookies: dict[str, str] = {}
        if cred.get("pass_ticket"):
            cookies["pass_ticket"] = str(cred["pass_ticket"]).strip()
        if cred.get("uin"):
            cookies["wxuin"] = str(cred["uin"]).strip()
        sess = session or requests.Session()
        sess.trust_env = False
        resp = sess.get(
            GETMSG_URL, params=params, headers=headers, cookies=cookies, timeout=timeout
        )
        return _parse_profile_nickname(resp.text)
    except Exception:  # noqa: BLE001
        return ""


ProgressCb = Callable[[str], None]


def _page_newest_ts(batch: list[dict[str, Any]]) -> int:
    newest = 0
    for a in batch:
        ts = int(a.get("publish_ts") or 0)
        if ts > newest:
            newest = ts
    return newest


def _dedupe(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for a in articles:
        mid = str(a.get("mid") or "")
        idx = str(a.get("idx") or "")
        sn = str(a.get("sn") or "")
        if mid:
            key = f"mid:{mid}|idx:{idx or '1'}|sn:{sn}"
        else:
            key = str(a.get("identity") or "") or article_identity(
                str(a.get("link") or ""),
                title=str(a.get("title") or ""),
                publish_ts=int(a.get("publish_ts") or 0),
            )
        if not key:
            key = f"t:{a.get('title')}|ts:{a.get('publish_ts')}|l:{a.get('link')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def _sighting_to_article(s: dict[str, Any]) -> dict[str, Any] | None:
    link = _clean_url(str(s.get("link") or ""))
    title = str(s.get("title") or "").strip() or "(无标题)"
    if not link and title == "(无标题)":
        return None
    publish_ts = int(s.get("publish_ts") or 0)
    mid, idx, sn = _mid_idx_sn(link)
    if not mid:
        mid = str(s.get("mid") or "")
        idx = str(s.get("idx") or idx or "1")
        sn = str(s.get("sn") or sn)
    identity = str(s.get("identity") or "") or article_identity(
        link, title=title, publish_ts=publish_ts
    )
    return {
        "title": title,
        "link": link,
        "digest": str(s.get("digest") or "").strip(),
        "cover": _clean_url(str(s.get("cover") or "")),
        "author": str(s.get("author") or "").strip(),
        "publish_ts": publish_ts,
        "publish_at": str(s.get("publish_at") or "")
        or (
            datetime.fromtimestamp(publish_ts).strftime("%Y-%m-%d %H:%M")
            if publish_ts
            else ""
        ),
        "mid": mid,
        "idx": idx or "1",
        "sn": sn,
        "identity": identity,
        "source": str(s.get("source") or "sighting"),
    }


def merge_articles_with_sightings(
    base: list[dict[str, Any]],
    sightings: list[dict[str, Any]],
    *,
    cutoff_ts: int = 0,
    end_ts: int = 0,
    biz: str = "",
) -> list[dict[str, Any]]:
    """Merge getmsg articles with MITM/manual sightings (fills API gaps).

    WeChat often omits later same-day pushes from ``getmsg``. Sightings collected
    while browsing (or补录) are merged in and deduped by mid|idx|sn / identity.
    ``cutoff_ts``/``end_ts`` 为闭区间下/上限（0 = 不限）；publish_ts 未知的行保留。
    """
    rows: list[dict[str, Any]] = []
    for a in base or []:
        row = dict(a)
        row.setdefault("source", "getmsg")
        rows.append(row)

    biz = (biz or "").strip()
    for s in sightings or []:
        if biz:
            sb = str(s.get("__biz") or "").strip()
            link_biz = ""
            try:
                q = parse_qs(urlparse(_clean_url(str(s.get("link") or ""))).query)
                link_biz = unquote((q.get("__biz") or [""])[0])
            except Exception:
                link_biz = ""
            if sb and sb != biz:
                continue
            if not sb and link_biz and link_biz != biz:
                continue
        art = _sighting_to_article(s)
        if not art:
            continue
        ts = int(art.get("publish_ts") or 0)
        if cutoff_ts and ts and ts < cutoff_ts:
            continue
        if end_ts and ts and ts > end_ts:
            continue
        rows.append(art)

    def _in_window(ts: int) -> bool:
        if not ts:
            return True
        if cutoff_ts and ts < cutoff_ts:
            return False
        if end_ts and ts > end_ts:
            return False
        return True

    merged = _dedupe(rows)
    merged = [a for a in merged if _in_window(int(a.get("publish_ts") or 0))]
    merged.sort(key=lambda a: int(a.get("publish_ts") or 0), reverse=True)
    return merged


def fetch_history_range(
    cred: dict[str, Any],
    *,
    start_ts: int,
    end_ts: int = 0,
    days: int = 0,
    max_pages: int = DEFAULT_MAX_PAGES,
    count: int = DEFAULT_PAGE_COUNT,
    sleep_s: float = 1.2,
    on_progress: ProgressCb | None = None,
    sightings: list[dict[str, Any]] | None = None,
    known_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Paginate getmsg and keep articles with publish_ts in ``[start_ts, end_ts]``.

    ``end_ts`` 为 0 表示不设上限（默认到今天）。getmsg ``count`` 是推送条数，
    一条推送可能含多篇文章（multi-appmsg）。翻页直到整页都老于 start_ts
    （或微信说没有了 / 达到 max_pages）。比 end_ts 新的文章跳过但继续翻页。

    ``days`` 仅用于进度/告警文案与返回值（0 = 自定义日期范围）。

    ``sightings`` (MITM browse / 补录) are merged in because WeChat often omits
    later same-day pushes from getmsg alone.

    ``known_keys``（identity/link 集合，2026-09 断点拉取）：**某页窗口内的文章
    全部已在集合里就停止继续翻页**。列表是从新往旧翻的，所以「这一页全是老熟人」
    意味着更旧的页在上一次拉取时已经翻过了。

    不存 ``offset`` 做续翻：offset 是「推送列表里的位置序号」，微信一发新推送，
    所有历史文章的位置整体后移，存下来的值下次就对不上。按「内容是否已知」判断
    不受这种漂移影响。
    """
    cred = normalize_credentials(cred)
    ok, err = validate_credentials(cred)
    if not ok:
        return {"ok": False, "error": err, "articles": [], "pages": 0}

    start_ts = max(0, int(start_ts))
    end_ts = max(0, int(end_ts))
    if end_ts and end_ts < start_ts:
        end_ts = start_ts
    scope = f"近 {days} 天" if days else "所选日期范围"
    articles: list[dict[str, Any]] = []
    pages = 0
    offset = 0
    nickname = ""
    sess = requests.Session()
    sess.trust_env = False
    hit_page_cap = False
    last_can_continue = False
    stopped_early = False
    known = known_keys or set()
    biz = str(cred.get("__biz") or "").strip()

    page_limit = max(1, int(max_pages))
    for i in range(page_limit):
        if on_progress:
            on_progress(f"正在拉取第 {i + 1} 页（已收录 {len(articles)} 篇）…")
        page = fetch_getmsg_page(cred, offset=offset, count=count, session=sess)
        pages += 1
        if not page.get("ok"):
            partial = merge_articles_with_sightings(
                articles, sightings or [], cutoff_ts=start_ts, end_ts=end_ts, biz=biz
            )
            return {
                "ok": False,
                "error": page.get("error") or "getmsg 失败",
                "articles": partial,
                "pages": pages,
                "days": days,
                "cutoff_ts": start_ts,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "warning": "",
                "truncated": False,
                "notice": "",
                "stopped_early": False,
                "nickname": nickname,
                "merged_sightings": max(0, len(partial) - len(_dedupe(articles))),
            }

        batch = page.get("articles") or []
        if not nickname:
            nickname = str(page.get("nickname") or "").strip()
        last_can_continue = bool(page.get("can_continue"))
        raw_msg_count = 0
        try:
            raw = page.get("raw") or {}
            gml = raw.get("general_msg_list") or ""
            if isinstance(gml, str):
                gml_obj = json.loads(gml) if gml.strip() else {}
            else:
                gml_obj = gml if isinstance(gml, dict) else {}
            raw_msg_count = len(gml_obj.get("list") or [])
        except Exception:
            raw_msg_count = len(batch)

        in_window = 0
        in_window_known = 0
        for a in batch:
            ts = int(a.get("publish_ts") or 0)
            if ts and ts < start_ts:
                continue
            if end_ts and ts and ts > end_ts:
                continue
            in_window += 1
            if known and (
                str(a.get("identity") or "") in known or str(a.get("link") or "") in known
            ):
                in_window_known += 1
            row = dict(a)
            row.setdefault("source", "getmsg")
            articles.append(row)

        newest = _page_newest_ts(batch)
        page_fully_old = bool(batch) and newest > 0 and newest < start_ts

        if page_fully_old:
            break

        # 断点拉取：这一页窗口内的文章全都已入库 → 更旧的页上次已翻过，不必再请求。
        # 只认「窗口内」的文章：比 end_ts 新的文章本轮本就要跳过，不能因为它们
        # 未知就一路翻下去；窗口内一篇都没有时也无从判断，继续翻。
        if known and in_window > 0 and in_window_known == in_window:
            stopped_early = True
            break
        if raw_msg_count <= 0 and not batch:
            break

        nxt = page.get("next_offset")
        if nxt is None:
            nxt_i = offset + int(count)
        else:
            try:
                nxt_i = int(nxt)
            except Exception:
                break
        if nxt_i <= offset:
            break

        offset = nxt_i

        if i + 1 < page_limit:
            time.sleep(sleep_s)
        else:
            hit_page_cap = True
            last_can_continue = True

    if not nickname:
        nickname = fetch_profile_nickname(cred, session=sess)
    base_n = len(_dedupe(articles))
    deduped = merge_articles_with_sightings(
        articles, sightings or [], cutoff_ts=start_ts, end_ts=end_ts, biz=biz
    )
    merged_extra = max(0, len(deduped) - base_n)

    # 两类信息必须分开（2026-09 修复）：`truncated` 是**问题**（可能没拉完），
    # `notice` 是**好消息**（合并了补录/抓包的缺口）。原先都塞进 `warning`，
    # 前端一旦在成功分支显示 warning，就会把「已合并补录/抓包 1 篇」这种
    # 正常提示渲染成红色错误「拉取未完整」。
    truncation = ""
    if hit_page_cap and last_can_continue:
        truncation = (
            f"已达翻页上限 {page_limit} 页，{scope}内可能仍有文章未拉完；"
            "再次拉取会跳过已入库的部分，继续往下翻。"
        )
    notice = f"已合并补录/抓包 {merged_extra} 篇" if merged_extra else ""
    warn = " · ".join(x for x in (truncation, notice) if x)  # 兼容：仍给合并文本

    if on_progress:
        msg = f"完成：{scope}共 {len(deduped)} 篇（请求 {pages} 页）"
        if stopped_early:
            msg += " · 已到上次拉取的位置，未重复翻页"
        if warn:
            msg += f" · {warn}"
        on_progress(msg)

    return {
        "ok": True,
        "error": "",
        "warning": warn,
        "truncated": bool(truncation),
        "notice": notice,
        "articles": deduped,
        "pages": pages,
        "days": days,
        "cutoff_ts": start_ts,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "hit_page_cap": hit_page_cap,
        # 断点拉取命中：某页窗口内文章全部已入库 → 提前停止，没再打微信
        "stopped_early": stopped_early,
        "merged_sightings": merged_extra,
        "nickname": nickname,
        "__biz": biz,
    }


def fetch_history_days(
    cred: dict[str, Any],
    *,
    days: int = 7,
    max_pages: int = DEFAULT_MAX_PAGES,
    count: int = DEFAULT_PAGE_COUNT,
    sleep_s: float = 1.2,
    on_progress: ProgressCb | None = None,
    sightings: list[dict[str, Any]] | None = None,
    known_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Paginate getmsg and keep articles with publish_ts within the last ``days``.

    薄封装：cutoff = now - days*86400 后走 ``fetch_history_range``。
    ``known_keys`` 见 ``fetch_history_range``（断点拉取）。
    """
    days = max(1, int(days))
    cutoff = int(time.time()) - days * 86400
    return fetch_history_range(
        cred,
        start_ts=cutoff,
        days=days,
        max_pages=max_pages,
        count=count,
        sleep_s=sleep_s,
        on_progress=on_progress,
        sightings=sightings,
        known_keys=known_keys,
    )
