from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core.batch_import import (  # noqa: E402
    dedupe_against_existing,
    dedupe_by_name,
    parse_batch_file,
    parse_batch_lines,
    split_fresh_duplicates,
)

URL_A = "https://mp.weixin.qq.com/s/aaaa"
URL_B = "https://mp.weixin.qq.com/s/bbbb"


def _valid(entries):
    return [e for e in entries if not e.get("error")]


def test_same_line_name_url():
    entries = parse_batch_lines(f"傅里叶的猫 {URL_A}")
    assert len(_valid(entries)) == 1
    assert _valid(entries)[0]["name"] == "傅里叶的猫"
    assert _valid(entries)[0]["url"] == URL_A


def test_same_line_url_name():
    entries = parse_batch_lines(f"{URL_A} 新智元")
    assert _valid(entries)[0]["name"] == "新智元"
    assert _valid(entries)[0]["url"] == URL_A


def test_name_line_then_url_line():
    entries = parse_batch_lines(f"集微网\n{URL_A}")
    assert len(_valid(entries)) == 1
    assert _valid(entries)[0]["name"] == "集微网"
    assert _valid(entries)[0]["url"] == URL_A


def test_url_only_defaults_name():
    entries = parse_batch_lines(URL_A)
    assert len(_valid(entries)) == 1
    assert _valid(entries)[0]["name"] == "未命名公众号"


def test_invalid_line():
    entries = parse_batch_lines("这不是链接也不是名称对")
    assert entries and entries[0]["error"]
    assert entries[0]["url"] == ""


def test_batch_internal_dedupe():
    entries = parse_batch_lines(f"{URL_A}\n{URL_A}")
    valid = _valid(entries)
    dup = [e for e in entries if e.get("duplicate")]
    assert len(valid) == 1
    assert len(dup) == 1
    assert dup[0]["url"] == URL_A


def test_name_cleaning():
    entries = parse_batch_lines(f"名称：AI驱动FPGA {URL_A}")
    assert _valid(entries)[0]["name"] == "AI驱动FPGA"
    entries = parse_batch_lines(f"《芯师爷》 {URL_B}")
    assert _valid(entries)[0]["name"] == "芯师爷"


def test_mixed_formats():
    text = (
        f"傅里叶的猫 {URL_A}\n"
        f"新智元\n{URL_B}\n"
        f"{URL_A}\n"
        "无效行文本"
    )
    entries = parse_batch_lines(text)
    valid = _valid(entries)
    dup = [e for e in entries if e.get("duplicate")]
    bad = [e for e in entries if e.get("error") and not e.get("duplicate")]
    assert len(valid) == 2  # aaaa(傅里叶) + bbbb(新智元)
    assert len(dup) == 1  # 第二个 aaaa（未命名公众号）
    assert len(bad) == 1  # 末尾无效行


def test_dedupe_against_existing():
    entries = parse_batch_lines(f"{URL_A}\n{URL_B}")
    fresh, duplicates = dedupe_against_existing(entries, {URL_A})
    assert len(fresh) == 1
    assert fresh[0]["url"] == URL_B
    assert len(duplicates) == 1
    assert duplicates[0]["url"] == URL_A


def test_url_line_then_name_line():
    entries = parse_batch_lines(f"{URL_A}\n傅里叶的猫")
    valid = _valid(entries)
    assert len(valid) == 1
    assert valid[0]["name"] == "傅里叶的猫"
    assert valid[0]["url"] == URL_A


def test_url_line_followed_by_url_line_both_unnamed():
    entries = parse_batch_lines(f"{URL_A}\n{URL_B}")
    valid = _valid(entries)
    assert len(valid) == 2
    assert {e["url"] for e in valid} == {URL_A, URL_B}


def test_dedupe_by_name_keeps_first():
    entries = parse_batch_lines(
        f"傅里叶的猫 {URL_A}\n傅里叶的猫 {URL_B}\n新智元 {URL_C}"
    )
    out = dedupe_by_name(entries)
    fresh = [e for e in out if not e.get("duplicate") and e.get("url")]
    dups = [e for e in out if e.get("duplicate") and e.get("url")]
    assert len(fresh) == 2  # 傅里叶的猫(URL_A) + 新智元(URL_C)
    assert len(dups) == 1  # 同名傅里叶的猫(URL_B)
    assert fresh[0]["name"] == "傅里叶的猫"
    assert fresh[0]["url"] == URL_A


def test_dedupe_by_name_ignores_unnamed():
    entries = parse_batch_lines(f"{URL_A}\n{URL_B}")
    out = dedupe_by_name(entries)
    fresh = [e for e in out if not e.get("duplicate") and e.get("url")]
    assert len(fresh) == 2


URL_C = "https://mp.weixin.qq.com/s/cccc"


def test_split_fresh_duplicates_url_and_name():
    entries = parse_batch_lines(
        f"{URL_A}\n傅里叶的猫 {URL_B}\n新智元 {URL_C}"
    )
    fresh, dup_urls, dup_names = split_fresh_duplicates(
        entries, {URL_A}, {"傅里叶的猫"}
    )
    assert len(fresh) == 1
    assert fresh[0]["name"] == "新智元"
    assert fresh[0]["url"] == URL_C
    assert [e["url"] for e in dup_urls] == [URL_A]
    assert [e["name"] for e in dup_names] == ["傅里叶的猫"]


def test_parse_batch_file_txt():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.txt"
        p.write_text(f"傅里叶的猫 {URL_A}\n新智元\n{URL_B}", encoding="utf-8")
        entries = parse_batch_file(p)
        valid = _valid(entries)
        assert len(valid) == 2
        assert (valid[0]["name"], valid[0]["url"]) == ("傅里叶的猫", URL_A)
        assert (valid[1]["name"], valid[1]["url"]) == ("新智元", URL_B)


def test_parse_batch_file_csv():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.csv"
        p.write_text(
            "名称,链接\n傅里叶的猫," + URL_A + "\n新智元," + URL_B + "\n",
            encoding="utf-8",
        )
        entries = parse_batch_file(p)
        valid = _valid(entries)
        assert len(valid) == 2
        assert (valid[0]["name"], valid[0]["url"]) == ("傅里叶的猫", URL_A)


def test_parse_batch_file_csv_url_first():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.csv"
        p.write_text(
            "文章链接,公众号\n" + URL_A + ",傅里叶的猫\n",
            encoding="utf-8",
        )
        entries = parse_batch_file(p)
        valid = _valid(entries)
        assert (valid[0]["name"], valid[0]["url"]) == ("傅里叶的猫", URL_A)


def test_parse_batch_file_json():
    import json

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        p.write_text(
            json.dumps(
                {"accounts": [{"name": "傅里叶的猫", "url": URL_A}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        entries = parse_batch_file(p)
        valid = _valid(entries)
        assert (valid[0]["name"], valid[0]["url"]) == ("傅里叶的猫", URL_A)


def test_parse_batch_file_missing():
    with tempfile.TemporaryDirectory() as td:
        entries = parse_batch_file(Path(td) / "nope.txt")
        assert entries and entries[0]["error"]
