"""批量导入公众号：把粘贴文本或文件解析为（名称, 链接）条目。

支持的输入格式：
  1) 同行：`公众号名称 https://mp.weixin.qq.com/s/xxx` 或 `链接 公众号名称`；
  2) 名称一行、链接下一行（正向配对）；
  3) 链接一行、名称下一行（反向配对，新增）；
  4) 纯链接行：名称为默认值「未命名公众号」；
  5) 无效行：既无链接、前后也无链接 → error 说明。

去重：
  - 批次内按 URL 去重（后出现的标记 duplicate=True）；
  - `dedupe_by_name` 做批内同名去重（同一公众号多篇文章只保留一个）；
  - `split_fresh_duplicates` 同时对已添加列表做 URL 与名称双重去重；
  - `dedupe_against_existing` 保持旧签名（仅 URL 去重），供兼容调用。

文件导入：`parse_batch_file` 支持 .txt / .csv / .json。
本模块为纯逻辑，不依赖 customtkinter/tkinter，可在任意 Python 环境测试。
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from mp_harvest.core.credentials import find_mp_url

DEFAULT_NAME = "未命名公众号"

# 去掉「名称：」「公众号：」等前缀，以及引号/书名号/括号等装饰符号
_NAME_PREFIX_RE = re.compile(r"^(?:名称|公众号|账号|帐号)\s*[:：]\s*")
_TRIM_CHARS = ' \t\u3000"\'“”‘’《》〈〉【】[]（）()'

_CSV_HEADER_ALIASES = {
    "名称",
    "公众号",
    "公众号名称",
    "账号",
    "帐号",
    "account",
    "name",
    "nickname",
    "链接",
    "文章链接",
    "公众号链接",
    "link",
    "url",
    "article_url",
}


def _clean_name(raw: str) -> str:
    name = _NAME_PREFIX_RE.sub("", raw or "")
    return name.strip(_TRIM_CHARS)


def _line_has_url(line: str) -> bool:
    return bool(find_mp_url(line))


def _make_entry(
    name: str, url: str, line_no: int, seen_urls: set[str]
) -> dict:
    url = str(url or "").strip()
    entry = {
        "name": _clean_name(name) or DEFAULT_NAME,
        "url": url,
        "error": "",
        "duplicate": False,
    }
    if url in seen_urls:
        entry["duplicate"] = True
        entry["error"] = f"第 {line_no + 1} 行：与批次内更早的链接重复"
    else:
        seen_urls.add(url)
    return entry


def parse_batch_lines(text: str) -> list[dict]:
    """把多行文本解析成条目列表。

    每条返回：
      {"name": str, "url": str, "error": str, "duplicate": bool}
    - error 非空表示该条目无效；
    - duplicate=True 表示与批次内更早的条目 URL 重复。
    """
    raw_lines = (text or "").splitlines()
    lines: list[tuple[int, str]] = [
        (i, ln.strip()) for i, ln in enumerate(raw_lines) if ln.strip()
    ]

    entries: list[dict] = []
    seen_urls: set[str] = set()
    pos = 0

    while pos < len(lines):
        line_no, line = lines[pos]
        url = find_mp_url(line)

        if url:
            name_part = _clean_name(line.replace(url, "").strip())
            if name_part:
                entries.append(_make_entry(name_part, url, line_no, seen_urls))
                pos += 1
                continue

            # 批次内重复的 URL：标记为重复，不再吞并下一行（避免把无效行当名称）
            if url in seen_urls:
                entries.append(_make_entry(DEFAULT_NAME, url, line_no, seen_urls))
                pos += 1
                continue

            # URL 行没有名称：看下一行是否为纯名称行（反向配对：链接→名称）
            if pos + 1 < len(lines):
                nxt_no, nxt = lines[pos + 1]
                if not _line_has_url(nxt):
                    nxt_name = _clean_name(nxt)
                    if nxt_name:
                        entries.append(_make_entry(nxt_name, url, line_no, seen_urls))
                        pos += 2
                        continue

            entries.append(_make_entry(DEFAULT_NAME, url, line_no, seen_urls))
            pos += 1
            continue

        # 无链接：可能是名称行，向后找第一条带链接的行（正向配对）
        next_idx = pos + 1
        while next_idx < len(lines) and not _line_has_url(lines[next_idx][1]):
            next_idx += 1
        if next_idx < len(lines):
            nxt_no, nxt = lines[next_idx]
            nxt_url = find_mp_url(nxt).strip()
            name = _clean_name(line) or DEFAULT_NAME
            entries.append(_make_entry(name, nxt_url, line_no, seen_urls))
            pos = next_idx + 1
            continue

        # 名称行后面没有链接 → 无效
        entries.append(
            {
                "name": _clean_name(line) or line,
                "url": "",
                "error": f"第 {line_no + 1} 行：没有找到公众号文章链接",
                "duplicate": False,
            }
        )
        pos += 1

    return entries


# ── 文件导入 ────────────────────────────────────────────────────────


def _error_entry(message: str) -> list[dict]:
    return [
        {"name": "", "url": "", "error": message, "duplicate": False}
    ]


def _parse_csv_rows(rows: list[list[str]]) -> list[dict]:
    entries: list[dict] = []
    seen_urls: set[str] = set()
    if not rows:
        return entries

    header = [str(c or "").strip().lower().replace(" ", "") for c in rows[0]]
    has_header = any(h in _CSV_HEADER_ALIASES for h in header)
    body = rows[1:] if has_header else rows

    for row_idx, row in enumerate(body):
        cells = [str(c or "").strip() for c in row]
        if not any(cells):
            continue
        line_no = row_idx + (2 if has_header else 1)
        joined = " ".join(cells)
        url = find_mp_url(joined)
        if not url:
            entries.append(
                {
                    "name": _clean_name(joined) or joined,
                    "url": "",
                    "error": f"第 {line_no} 行：没有找到公众号文章链接",
                    "duplicate": False,
                }
            )
            continue

        name = ""
        for cell in cells:
            if not cell:
                continue
            if cell == url or url in cell:
                part = _clean_name(cell.replace(url, "").strip())
                if part:
                    name = part
            elif not find_mp_url(cell):
                name = _clean_name(cell)
                break
        entries.append(_make_entry(name or DEFAULT_NAME, url, line_no, seen_urls))
    return entries


def _parse_csv_file(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = [
            row
            for row in csv.reader(fh)
            if any(str(c or "").strip() for c in row)
        ]
    return _parse_csv_rows(rows)


def _parse_json_file(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        for key in ("items", "accounts", "list", "articles"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return _error_entry("JSON 顶层必须是数组或包含 items/accounts/list 列表")

    entries: list[dict] = []
    seen_urls: set[str] = set()
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            entries.append(
                {
                    "name": "",
                    "url": "",
                    "error": f"第 {idx + 1} 项：不是 JSON 对象",
                    "duplicate": False,
                }
            )
            continue
        name = _clean_name(
            str(
                item.get("name")
                or item.get("nickname")
                or item.get("account")
                or item.get("公众号")
                or ""
            )
        )
        url = str(
            item.get("url")
            or item.get("link")
            or item.get("article_url")
            or ""
        ).strip()
        if not url:
            entries.append(
                {
                    "name": name or DEFAULT_NAME,
                    "url": "",
                    "error": f"第 {idx + 1} 项：缺少 url/link",
                    "duplicate": False,
                }
            )
            continue
        entries.append(_make_entry(name or DEFAULT_NAME, url, idx, seen_urls))
    return entries


def parse_batch_file(path: str | Path) -> list[dict]:
    """从文件读取批量导入条目，支持 .txt / .csv / .json。

    读取/解码失败时返回单条 error 条目，便于 UI 直接展示。
    """
    path = Path(path)
    if not path.exists():
        return _error_entry(f"文件不存在：{path}")
    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            return _parse_csv_file(path)
        if suffix == ".json":
            return _parse_json_file(path)
        text = path.read_text(encoding="utf-8-sig")
    except Exception as exc:  # noqa: BLE001
        return _error_entry(f"无法读取文件：{exc}")
    return parse_batch_lines(text)


# ── 去重 ────────────────────────────────────────────────────────────


def _dedup_name_key(name: str) -> str:
    """名称去重键：清理后名称；空名与「未命名公众号」不做同名去重。"""
    key = _clean_name(name or "")
    if not key or key == DEFAULT_NAME:
        return ""
    return key


def dedupe_by_name(entries: list[dict]) -> list[dict]:
    """批内同名去重：同一清理后名称只保留第一个含链接条目。

    保留条目顺序；后续同名条目标记 duplicate=True 并写明原因。
    「未命名公众号」（纯链接行）不做同名合并。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for e in entries:
        if not e.get("url") or e.get("duplicate") or e.get("error"):
            out.append(e)
            continue
        key = _dedup_name_key(str(e.get("name") or ""))
        if not key:
            out.append(e)
            continue
        if key in seen:
            dup = dict(e)
            dup["duplicate"] = True
            dup["error"] = (
                f"同名公众号「{e.get('name')}」已导入，"
                f"跳过重复（链接 {e.get('url')}）"
            )
            out.append(dup)
        else:
            seen.add(key)
            out.append(e)
    return out


def split_fresh_duplicates(
    entries: list[dict],
    existing_urls: set[str],
    existing_names: set[str],
) -> tuple[list[dict], list[dict], list[dict]]:
    """与已添加列表做 URL + 名称双重去重。

    返回 (fresh, dup_urls, dup_names)：
    - fresh：可导入条目（有链接、非批内重复、URL 与名称均不与已有列表冲突）；
    - dup_urls：批内重复或 URL 已存在；
    - dup_names：同名公众号已存在（含本批次内更早出现的同名）。
    无效条目（无链接）不参与，由调用方按 error 统计。
    """
    known_urls = {str(u or "").strip() for u in existing_urls}
    known_names = {
        _dedup_name_key(str(n or ""))
        for n in existing_names
        if _dedup_name_key(str(n or ""))
    }
    fresh: list[dict] = []
    dup_urls: list[dict] = []
    dup_names: list[dict] = []
    seen_names: set[str] = set()

    for e in entries:
        url = str(e.get("url") or "").strip()
        if not url:
            continue
        if e.get("duplicate") or url in known_urls:
            dup_urls.append(e)
            continue
        name_key = _dedup_name_key(str(e.get("name") or ""))
        if name_key and (name_key in known_names or name_key in seen_names):
            dup_names.append(e)
            continue
        if name_key:
            seen_names.add(name_key)
        fresh.append(e)
    return fresh, dup_urls, dup_names


def dedupe_against_existing(
    entries: list[dict], existing_urls: set[str]
) -> tuple[list[dict], list[dict]]:
    """兼容旧调用：仅按已有 URL 过滤（不做同名去重）。"""
    known = {str(u or "").strip() for u in existing_urls}
    fresh: list[dict] = []
    duplicates: list[dict] = []
    for e in entries:
        url = str(e.get("url") or "").strip()
        if not url:
            continue
        if e.get("duplicate") or url in known:
            duplicates.append(e)
        else:
            fresh.append(e)
    return fresh, duplicates
