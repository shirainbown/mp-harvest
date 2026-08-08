"""Read WeChat MP article HTML and export (inspired by wechat-article-exporter).

正文导出**只有 HTML**（设计稿 §6）：
- 单文件自包含模板 ``templates/article.html``（Jinja2 渲染，内联 CSS + 暗色媒体查询）；
- 正文经白名单 sanitize（剥离 script/iframe/微信跟踪参数），data-src → src，
  图片统一 ``referrerpolicy="no-referrer"``；
- 批量导出逐篇生成 HTML + ``index.html`` 目录页；可选「下载图片到本地」
  （``download_images=True``，图片存 ``assets/`` 并改写为相对路径）。

v1.7.7 的 docx / markdown / txt / json 输出分支已删除（python-docx 依赖一并移除）。
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag
from jinja2 import Environment, FileSystemLoader

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "WindowsWechat(0x63090a13) XWEB/11275"
)

ARTICLE_EXPORT_FORMATS: dict[str, str] = {
    "html": "HTML",
}

ARTICLE_EXPORT_LABELS = list(ARTICLE_EXPORT_FORMATS.values())

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_JINJA = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _fully_unquote(value: str) -> str:
    s = value or ""
    for _ in range(3):
        n = unquote(s)
        if n == s:
            break
        s = n
    return s


def _html_to_text(fragment: str) -> str:
    if not fragment:
        return ""
    soup = BeautifulSoup(fragment, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _extract_publish_ts(html_text: str) -> int:
    for pattern in (
        r'var\s+ct\s*=\s*"(\d+)"',
        r'var\s+createTime\s*=\s*[\'"](\d+)[\'"]',
        r'publish_time\s*[:=]\s*[\'"]?(\d{10})',
        r'content_noencode.*?createTime\s*[:=]\s*[\'"]?(\d{10})',
    ):
        m = re.search(pattern, html_text or "", re.I | re.S)
        if m:
            try:
                ts = int(m.group(1))
                if ts > 1_000_000_000:
                    return ts
            except Exception:
                continue
    return 0


def parse_wechat_article_html(
    html_text: str,
    *,
    source_url: str = "",
) -> dict[str, Any]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    title = ""
    title_el = soup.select_one("#activity-name") or soup.select_one("h1.rich_media_title")
    if title_el:
        title = title_el.get_text(strip=True)
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = title or str(og_title["content"]).strip()

    content = soup.select_one("#js_content") or soup.select_one("div.rich_media_content")
    body_html = str(content) if content else ""
    body_text = _html_to_text(body_html) if body_html else _html_to_text(html_text or "")

    if len(body_text) < 20:
        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            desc = str(og_desc["content"]).strip()
            if len(desc) > len(body_text):
                body_text = desc

    publish_ts = _extract_publish_ts(html_text or "")
    publish_at = (
        datetime.fromtimestamp(publish_ts).strftime("%Y-%m-%d %H:%M")
        if publish_ts
        else ""
    )

    return {
        "title": title or "(无标题)",
        "body_text": body_text,
        "body_html": body_html or body_text,
        "link": source_url or "",
        "publish_ts": publish_ts,
        "publish_at": publish_at,
    }


# ── 白名单 sanitize（设计稿 §6.2）─────────────────────────────────────

ALLOWED_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "img", "blockquote", "pre", "code",
    "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "td", "th",
    "a", "strong", "b", "em", "i",
    "section", "span", "div", "br", "hr",
    "figure", "figcaption",
}

DROP_TAGS = {
    "script", "iframe", "style", "link", "meta", "noscript",
    "svg", "form", "input", "button", "textarea", "select",
    "object", "embed", "audio", "video",
}

_GLOBAL_ATTRS = {"class", "style"}
_TAG_ATTRS: dict[str, set[str]] = {
    "a": {"href"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}

# 微信链接里的跟踪参数（导出时剥离，不影响正文阅读）
TRACKING_PARAMS = {
    "chksm", "scene", "subscene", "clicktime", "enterid", "sessionid",
    "ascene", "realreporttime", "reporttime", "xtrack",
    "fasttmpl_type", "fasttmpl_fullversion", "fasttmpl_flag",
}


# 正文容器外的广告/二维码块（微信页面壳上的固定 id）
DROP_IDS = {
    "js_top_ad_area", "js_tags_preview_toast", "content_bottom_area",
    "js_pc_qr_code", "wx_stream_article_slide_tip",
}


def _strip_tracking_params(url: str) -> str:
    try:
        u = urlparse(url)
    except Exception:
        return url
    host = (u.hostname or "").lower()
    if not (host == "weixin.qq.com" or host.endswith(".weixin.qq.com")):
        return url
    if not u.query:
        return url
    kept = [
        (k, v)
        for k, v in parse_qsl(u.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    return urlunparse(u._replace(query=urlencode(kept)))


def sanitize_article_html(fragment: str) -> str:
    """白名单清洗正文片段：剥 script/iframe/事件属性/跟踪参数，data-src→src。"""
    if not fragment:
        return ""
    soup = BeautifulSoup(fragment, "html.parser")

    for tag in list(soup.find_all(True)):
        if not isinstance(tag, Tag) or tag.name is None:
            continue  # 已随父节点被 decompose
        name = tag.name.lower()

        # id 在属性白名单外，先基于 id 处理再清洗
        tag_id = str(tag.get("id") or "")
        if tag_id in DROP_IDS:
            tag.decompose()
            continue
        if tag_id == "js_content" and tag.has_attr("style"):
            # 微信默认 visibility:hidden 隐藏正文，导出时取消隐藏
            del tag["style"]

        if name in DROP_TAGS:
            tag.decompose()
            continue
        if name not in ALLOWED_TAGS:
            tag.unwrap()
            continue

        # data-src 不在属性白名单内，先提升为 src 再清洗（微信懒加载）
        if name == "img":
            data_src = str(tag.get("data-src") or "").strip()
            if data_src and not str(tag.get("src") or "").strip():
                tag["src"] = data_src

        allowed = _GLOBAL_ATTRS | _TAG_ATTRS.get(name, set())
        for attr in list(tag.attrs):
            low = attr.lower()
            if low.startswith("on") or low not in allowed:
                del tag[attr]

        if name == "img":
            src = str(tag.get("src") or tag.get("data-src") or "").strip()
            if src:
                tag["src"] = _strip_tracking_params(src)
                tag["referrerpolicy"] = "no-referrer"
            else:
                tag.decompose()
                continue
        elif name == "a":
            href = str(tag.get("href") or "").strip()
            if href and not href.lower().startswith(("javascript:", "data:")):
                tag["href"] = _strip_tracking_params(href)
                tag["target"] = "_blank"
                tag["rel"] = "noopener noreferrer"
            else:
                tag.unwrap()
                continue

    return "".join(str(child) for child in soup.contents)


# ── 图片本地化（可选设置项）────────────────────────────────────────────

_IMG_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|bmp)(?:$|\?)", re.I)


def _img_ext(src: str, content_type: str = "") -> str:
    m = re.search(r"wx_fmt=(\w+)", src or "")
    if m:
        return ".jpg" if m.group(1).lower() == "jpeg" else f".{m.group(1).lower()}"
    m = _IMG_EXT_RE.search(urlparse(src or "").path or "")
    if m:
        return "." + m.group(1).lower().replace("jpeg", "jpg")
    if "png" in content_type:
        return ".png"
    if "gif" in content_type:
        return ".gif"
    if "webp" in content_type:
        return ".webp"
    return ".jpg"


def localize_images(body_html: str, assets_dir: Path) -> str:
    """下载正文图片到 ``assets_dir``，src 改写为 ``<assets_dir.name>/<文件>`` 相对路径。

    下载失败的图片保留原 CDN 链接（不阻塞导出）。
    """
    if not body_html:
        return body_html
    soup = BeautifulSoup(body_html, "html.parser")
    imgs = [img for img in soup.find_all("img") if isinstance(img, Tag)]
    if not imgs:
        return body_html
    assets_dir.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://mp.weixin.qq.com/",
    }
    for n, img in enumerate(imgs, start=1):
        src = str(img.get("src") or "").strip()
        if not src.startswith(("http://", "https://")):
            continue
        try:
            sess = requests.Session()
            sess.trust_env = False
            resp = sess.get(src, headers=headers, timeout=20)
            resp.raise_for_status()
        except Exception:
            continue
        fname = f"img_{n:03d}{_img_ext(src, resp.headers.get('Content-Type', ''))}"
        try:
            (assets_dir / fname).write_bytes(resp.content)
        except Exception:
            continue
        img["src"] = f"{assets_dir.name}/{fname}"
    return str(soup)


# ── HTML 渲染与写盘 ────────────────────────────────────────────────────


def render_article_html(
    art: dict[str, Any],
    *,
    account: str = "",
    download_images: bool = False,
    assets_dir: Path | str | None = None,
) -> str:
    """用 templates/article.html 渲染单文件自包含 HTML（设计稿 §6.2）。"""
    title = str(art.get("title") or "(无标题)").strip()
    account = str(account or art.get("account") or "").strip()
    link = str(art.get("link") or "").strip()
    publish_at = str(art.get("publish_at") or "").strip()

    body_html = str(art.get("body_html") or "")
    if body_html.strip():
        body = sanitize_article_html(body_html)
    else:
        body = f"<pre>{html.escape(str(art.get('body_text') or ''))}</pre>"

    if download_images:
        body = localize_images(body, Path(assets_dir) if assets_dir else Path("assets"))

    template = _JINJA.get_template("article.html")
    return template.render(
        title=title,
        account=account,
        publish_at=publish_at,
        link=link,
        body=body,
    )


def write_article_export(
    path: Path | str,
    art: dict[str, Any],
    *,
    account: str = "",
    download_images: bool = False,
    assets_dir: Path | str | None = None,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_article_html(
            art,
            account=account,
            download_images=download_images,
            assets_dir=assets_dir,
        ),
        encoding="utf-8",
    )
    return path


def safe_export_filename(title: str, *, ext: str, index: int = 0) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', "_", (title or "article").strip())[:48] or "article"
    if index > 0:
        return f"{index:02d}_{safe}.{ext}"
    return f"{safe}.{ext}"


def _render_index_page(
    rows: list[dict[str, Any]],
    *,
    account_name: str = "",
) -> str:
    """批量导出目录页：标题/日期/链接表格，可点击跳各篇（设计稿 §6.1）。"""
    title = f"{account_name} · 文章目录" if account_name else "文章目录"
    trs = []
    for i, r in enumerate(rows, start=1):
        t = html.escape(str(r.get("title") or "(无标题)"))
        when = html.escape(str(r.get("publish_at") or ""))
        file = html.escape(str(r.get("file") or ""))
        link = html.escape(str(r.get("link") or ""))
        origin = f'<a href="{link}">原文</a>' if link else ""
        trs.append(
            f"<tr><td class='idx'>{i}</td>"
            f"<td><a href='{file}'>{t}</a></td>"
            f"<td class='when'>{when}</td>"
            f"<td>{origin}</td></tr>"
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{html.escape(title)}</title>
<style>
 body{{max-width:880px;margin:40px auto;padding:0 20px;
      font:15px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",serif;
      color:#1f2328;background:#fff}}
 table{{width:100%;border-collapse:collapse}}
 td,th{{padding:8px 10px;border-bottom:1px solid #e3e1dc;text-align:left;
       vertical-align:top}}
 th{{font-size:13px;color:#6b7280}}
 .idx,.when{{color:#6b7280;font-size:13px;white-space:nowrap}}
 a{{color:#0969da;text-decoration:none}}
 a:hover{{text-decoration:underline}}
 @media(prefers-color-scheme:dark){{body{{background:#1b1d1f;color:#e8eaed}}
  td,th{{border-color:#36383b}}.idx,.when,th{{color:#9ba0a6}}a{{color:#58a6ff}}}}
</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<p class="when">共 {len(rows)} 篇</p>
<table>
<tr><th>#</th><th>标题</th><th>日期</th><th>原文</th></tr>
{"".join(trs)}
</table>
</body>
</html>
"""


def batch_export_articles(
    articles: list[dict[str, Any]],
    *,
    out_dir: Path | str,
    fetch_article: Callable[..., dict[str, Any]] | None = None,
    cred: dict[str, Any] | None = None,
    account_name: str = "",
    download_images: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Fetch each article body and write one HTML per article + index.html 目录页.

    ``fetch_article(url, cred=...)`` defaults to ``fetch_and_parse_article``.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fetch = fetch_article or fetch_and_parse_article
    ok_n = 0
    failed_n = 0
    errors: list[str] = []
    written: list[str] = []
    index_rows: list[dict[str, Any]] = []

    for i, row in enumerate(articles, start=1):
        link = str(row.get("link") or "").strip()
        title = str(row.get("title") or f"article_{i}").strip()
        if on_progress:
            on_progress(f"正在导出 {i}/{len(articles)}：{title[:28]}")
        if not link:
            failed_n += 1
            errors.append(f"{title}: 无链接")
            continue
        try:
            parsed = fetch(link, cred=cred)
            if not parsed.get("publish_at") and row.get("publish_at"):
                parsed["publish_at"] = row.get("publish_at")
            if not parsed.get("publish_ts") and row.get("publish_ts"):
                parsed["publish_ts"] = row.get("publish_ts")
            if not parsed.get("title") or parsed.get("title") == "(无标题)":
                parsed["title"] = title or parsed.get("title")
            final_title = str(parsed.get("title") or title)
            fname = safe_export_filename(final_title, ext="html", index=i)
            path = write_article_export(
                out_dir / fname,
                parsed,
                account=account_name or str(row.get("account") or ""),
                download_images=download_images,
                assets_dir=out_dir / "assets",
            )
            written.append(str(path))
            index_rows.append(
                {
                    "title": final_title,
                    "publish_at": str(parsed.get("publish_at") or ""),
                    "file": fname,
                    "link": link,
                }
            )
            ok_n += 1
        except Exception as exc:  # noqa: BLE001
            failed_n += 1
            errors.append(f"{title}: {exc}")

    index_path = out_dir / "index.html"
    index_path.write_text(
        _render_index_page(index_rows, account_name=account_name),
        encoding="utf-8",
    )

    return {
        "ok": ok_n,
        "failed": failed_n,
        "errors": errors,
        "written": written,
        "out_dir": str(out_dir),
        "fmt": "html",
        "index": str(index_path),
    }


def fetch_article_html(
    url: str,
    *,
    cred: dict[str, Any] | None = None,
    timeout: float = 25.0,
    session: requests.Session | None = None,
) -> str:
    """Fetch article page HTML (direct to WeChat, bypass system proxy)."""
    url = (url or "").strip()
    if not url:
        raise ValueError("文章链接为空")

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://mp.weixin.qq.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    cookies: dict[str, str] = {}
    if cred:
        pt = str(cred.get("pass_ticket") or "").strip()
        uin = str(cred.get("uin") or "").strip()
        if pt:
            cookies["pass_ticket"] = _fully_unquote(pt)
        if uin:
            cookies["wxuin"] = _fully_unquote(uin)

    sess = session or requests.Session()
    sess.trust_env = False
    resp = sess.get(url, headers=headers, cookies=cookies, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
    return resp.text


def fetch_and_parse_article(
    url: str,
    *,
    cred: dict[str, Any] | None = None,
    timeout: float = 25.0,
) -> dict[str, Any]:
    html_text = fetch_article_html(url, cred=cred, timeout=timeout)
    return parse_wechat_article_html(html_text, source_url=url)
