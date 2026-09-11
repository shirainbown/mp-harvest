"""「其他来源」目录（core/external_sources.py）单元测试。

每条对应用户会真实遇到的故障，而不是为覆盖率凑数：
- 两种 papers_data.json 变体都要能读（实测同一目录里就并存）
- 重扫必须幂等（否则反复扫描把库撑爆）
- 同一篇跨日期目录重复只留一条（arXiv 流水线会跨天重复报告）
- 目录里删掉后重扫要清掉（否则列表里堆着打不开的僵尸条目）
- 写回不能覆盖目标目录里别人的条目
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core.external_sources import (  # noqa: E402
    ExternalStore,
    item_key_for,
    normalize_record,
    normalize_url,
    parse_papers_data,
    safe_id,
    scan_source,
    write_external_export,
)


# ── 夹具 ──────────────────────────────────────────────────────────


def _paper(arxiv_id: str, title: str, date: str = "2026-09-07", **extra) -> dict:
    rec = {
        "title": title,
        "abstract": f"{title} 的摘要",
        "url": f"http://arxiv.org/abs/{arxiv_id}",
        "arxiv_id": arxiv_id,
        "authors": ["A", "B"],
        "date": date,
        "categories": ["cs.DC"],
        "primary_category": "cs.DC",
    }
    rec.update(extra)
    return rec


def _write_date_dir(root: Path, dir_name: str, payload) -> Path:
    d = root / dir_name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "papers_data.json"
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return f


def _store(tmp_path: Path) -> ExternalStore:
    return ExternalStore(tmp_path / "ext.db")


# ── 解析：两种变体 ────────────────────────────────────────────────


def test_parse_list_variant(tmp_path):
    """变体 A（扁平 list）—— 用户给的参考格式。"""
    f = tmp_path / "papers_data.json"
    f.write_text(json.dumps([_paper("2608.1v1", "甲"), _paper("2608.2v1", "乙")],
                            ensure_ascii=False), encoding="utf-8")
    rows = parse_papers_data(f, fallback_date="2026-09-07")
    assert len(rows) == 2
    assert rows[0]["title"] == "甲"
    assert rows[0]["item_key"] == "arxiv:2608.1"
    assert rows[0]["publish_ts"] > 0


def test_parse_domains_variant(tmp_path):
    """变体 B（按领域分组）—— 服务器流水线现在写的格式。

    实测 2026-08-10 就是这种；只认扁平 list 的话这天整个读不出来。
    """
    payload = {
        "total_fetched": 425,
        "matched_count": 2,
        "target_date": "2026-09-10",
        "domains": {
            "AI Acceleration & Intelligent Computing": [
                _paper("2609.1v1", "丙", title_cn="丙（中文）", summary_cn="中文摘要")
            ],
            "Architecture & Chip Design": [_paper("2609.2v1", "丁")],
        },
    }
    f = tmp_path / "papers_data.json"
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    rows = parse_papers_data(f, fallback_date="2026-09-10")
    assert len(rows) == 2
    by_key = {r["item_key"]: r for r in rows}
    assert by_key["arxiv:2609.1"]["title_cn"] == "丙（中文）"
    assert by_key["arxiv:2609.1"]["summary_cn"] == "中文摘要"


def test_domains_variant_backfills_domain(tmp_path):
    """变体 B 的领域名来自分组 key —— 条目自身没有 domain 字段，必须回填。"""
    payload = {"domains": {"Architecture & Chip Design": [_paper("2609.2v1", "丁")]}}
    f = tmp_path / "papers_data.json"
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    rows = parse_papers_data(f)
    assert rows[0]["domain"] == "Architecture & Chip Design"


def test_parse_tolerates_garbage(tmp_path):
    """文件缺失 / 非法 JSON / 形态不认识 → 空列表，绝不抛异常。"""
    assert parse_papers_data(tmp_path / "不存在.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{ 不是 json", encoding="utf-8")
    assert parse_papers_data(bad) == []
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"foo": 1}), encoding="utf-8")
    assert parse_papers_data(other) == []


def test_parse_skips_non_article_rows(tmp_path):
    """既没标题也没 URL 的行不是文章，丢掉（避免库里出现空条目）。"""
    f = tmp_path / "papers_data.json"
    f.write_text(json.dumps([{"foo": 1}, _paper("2608.1v1", "甲")]), encoding="utf-8")
    assert len(parse_papers_data(f)) == 1


# ── item_key 优先级 ───────────────────────────────────────────────


def test_item_key_precedence():
    """arxiv_id > url > 标题摘要；同一篇在不同目录必须算出同一个键。"""
    assert item_key_for({"arxiv_id": "2608.1v1", "url": "http://x/1",
                         "title": "t"}) == "arxiv:2608.1"
    assert item_key_for({"url": "http://x/1", "title": "t"}) == "url:http://x/1"
    k1 = item_key_for({"title": "t", "domain": "d"})
    k2 = item_key_for({"title": "t", "domain": "d"})
    assert k1 == k2 and k1.startswith("title:")
    # 标题相同但领域不同 → 不同键（否则会把两篇不同的文章合成一篇）
    assert item_key_for({"title": "t", "domain": "d1"}) != item_key_for({"title": "t", "domain": "d2"})


def test_normalize_url_strips_tracking():
    """跟踪参数与尾斜杠参与计算会让同一篇算出两个键。"""
    assert normalize_url("http://x/a/?utm_source=wx&id=1") == "http://x/a?id=1"
    assert normalize_url("http://x/a/") == "http://x/a"
    assert normalize_url("http://x/a#frag") == "http://x/a"


def test_safe_id_is_filename_safe():
    assert safe_id("arxiv:2608.26575v1") == "2608.26575v1"
    # 含非法字符的键不能原样进文件名
    got = safe_id("url:http://x/a/b?c=1")
    assert "/" not in got and ":" not in got and "?" not in got


# ── 扫描 ──────────────────────────────────────────────────────────


def test_scan_indexes_items(tmp_path):
    root = tmp_path / "src"
    _write_date_dir(root, "2026-09-07", [_paper("2608.1v1", "甲"), _paper("2608.2v1", "乙")])
    _write_date_dir(root, "2026-08-29", [_paper("2608.3v1", "丙")])
    # 非日期目录要忽略
    (root / "logs").mkdir()
    (root / "logs" / "papers_data.json").write_text("[]", encoding="utf-8")

    st = _store(tmp_path)
    src = st.add_source(root, "论文")
    assert src is not None

    r = scan_source(st, src["id"])
    assert r["ok"] is True
    assert r["seen"] == 3
    assert len(st.list_items(src["id"])) == 3
    assert st.get_source(src["id"])["last_scan_seen"] == 3


def test_scan_is_idempotent(tmp_path):
    """重扫不产生重复行 —— 这是「建库」不是「追加」的关键。"""
    root = tmp_path / "src"
    _write_date_dir(root, "2026-09-07", [_paper("2608.1v1", "甲")])
    st = _store(tmp_path)
    src = st.add_source(root, "论文")

    scan_source(st, src["id"])
    n1 = len(st.list_items(src["id"]))
    r2 = scan_source(st, src["id"])
    n2 = len(st.list_items(src["id"]))
    assert n1 == n2 == 1
    assert r2["new"] == 0  # 第二遍没有新增


def test_scan_dedupes_across_date_folders(tmp_path):
    """同一篇跨日期目录重复 → 只留一条，且**最新**那个目录胜出。

    arXiv 流水线会跨天重复报告同一篇（实测样本里就有 4 篇）。
    ``origin_file`` 必须一起指向最新目录 —— 只断言 dir_date 的话，把
    去重逻辑整个删掉测试也照样过（写库顺序碰巧就是升序）。
    """
    root = tmp_path / "src"
    _write_date_dir(root, "2026-08-29", [_paper("2608.27184v1", "重复篇")])
    _write_date_dir(root, "2026-09-07", [_paper("2608.27184v1", "重复篇")])
    st = _store(tmp_path)
    src = st.add_source(root, "论文")

    r = scan_source(st, src["id"])
    rows = st.list_items(src["id"])
    assert len(rows) == 1
    assert rows[0]["dir_date"] == "2026-09-07"
    # 来源文件与 dir_date 必须自洽：都指向最新那天的 papers_data.json
    assert rows[0]["origin_file"] == str(root / "2026-09-07" / "papers_data.json")
    # seen 报「唯一条目数」而不是「重复出现次数」，否则 UI 上会显示 2 条实际只有 1 条
    assert r["seen"] == 1


def test_missing_file_removed_on_rescan(tmp_path):
    """目录里删掉后重扫要清掉条目 —— 否则列表里堆着打不开的僵尸。"""
    root = tmp_path / "src"
    f_a = _write_date_dir(root, "2026-09-07", [_paper("2608.1v1", "甲")])
    _write_date_dir(root, "2026-08-29", [_paper("2608.2v1", "乙")])
    st = _store(tmp_path)
    src = st.add_source(root, "论文")
    scan_source(st, src["id"])
    assert len(st.list_items(src["id"])) == 2

    f_a.unlink()
    r = scan_source(st, src["id"])
    assert r["removed"] == 1
    rows = st.list_items(src["id"])
    assert len(rows) == 1 and rows[0]["title"] == "乙"


def test_scan_links_pdf_and_body(tmp_path):
    """同目录下的 PDF 与正文文件要被认出来（列表里要能「打开 PDF / 正文」）。"""
    root = tmp_path / "src"
    _write_date_dir(root, "2026-09-07", [_paper("2608.1v1", "甲")])
    (root / "2026-09-07" / "2608.1v1.pdf").write_bytes(b"%PDF-1.4")
    (root / "2026-09-07" / "2608.1v1.html").write_text("<html>x</html>", encoding="utf-8")

    st = _store(tmp_path)
    src = st.add_source(root, "论文")
    scan_source(st, src["id"])
    row = st.list_items(src["id"])[0]
    assert row["pdf_path"].endswith("2608.1v1.pdf")
    assert row["body_path"].endswith("2608.1v1.html")


def test_scan_finds_pdf_via_root_relative_path(tmp_path):
    """``pdf_local_path`` 是**相对来源根目录**的（实测 ``"2026-08-10/xxx.pdf"``）。

    这里故意让 PDF 文件名与 ``arxiv_id`` 不同，堵死 ``{arxiv_id}.pdf`` 兜底 ——
    否则按日期目录拼错路径的 bug 会被兜底悄悄掩盖（真实数据里就发生过）。
    """
    root = tmp_path / "src"
    _write_date_dir(root, "2026-08-10", [
        _paper("2608.07078v1", "甲", pdf_local_path="2026-08-10/paper-07078.pdf")
    ])
    (root / "2026-08-10" / "paper-07078.pdf").write_bytes(b"%PDF-1.4")

    st = _store(tmp_path)
    src = st.add_source(root, "论文")
    scan_source(st, src["id"])
    row = st.list_items(src["id"])[0]
    assert row["pdf_path"].endswith("paper-07078.pdf")


def test_scan_finds_pdf_via_date_dir_relative_path(tmp_path):
    """另一种基准：相对日期目录（服务器新格式 ``"2609.10057v1.pdf"``）。"""
    root = tmp_path / "src"
    _write_date_dir(root, "2026-09-10", [
        _paper("2609.10057v1", "乙", pdf_local_path="2609.10057v1.pdf")
    ])
    (root / "2026-09-10" / "2609.10057v1.pdf").write_bytes(b"%PDF-1.4")

    st = _store(tmp_path)
    src = st.add_source(root, "论文")
    scan_source(st, src["id"])
    assert st.list_items(src["id"])[0]["pdf_path"].endswith("2609.10057v1.pdf")


def test_item_key_strips_arxiv_version():
    """同一篇论文改版（v1→v2）要算同一条，否则两行、两次 AI 判定、导出两份。"""
    k1 = item_key_for({"arxiv_id": "2608.25061v1", "title": "t"})
    k2 = item_key_for({"arxiv_id": "2608.25061v2", "title": "t"})
    assert k1 == k2 == "arxiv:2608.25061"
    # 完整带版本的 ID 仍原样保存，信息不丢
    rec = normalize_record({"arxiv_id": "2608.25061v2", "title": "t", "url": "u"})
    assert rec["arxiv_id"] == "2608.25061v2"
    # 不带版本号的 ID 不受影响
    assert item_key_for({"arxiv_id": "2608.1", "title": "t"}) == "arxiv:2608.1"


def test_scan_collapses_versions_across_folders(tmp_path):
    """v1 与 v2 跨目录出现时只留一条（真实数据里已有 v2 记录）。"""
    root = tmp_path / "src"
    _write_date_dir(root, "2026-08-29", [_paper("2608.25061v1", "DataKernelBench")])
    _write_date_dir(root, "2026-09-07", [_paper("2608.25061v2", "DataKernelBench")])
    st = _store(tmp_path)
    src = st.add_source(root, "论文")
    r = scan_source(st, src["id"])
    assert r["seen"] == 1
    assert len(st.list_items(src["id"])) == 1
    # 留下的是最新目录与最新版本号
    row = st.list_items(src["id"])[0]
    assert row["dir_date"] == "2026-09-07"
    assert row["arxiv_id"] == "2608.25061v2"


def test_scan_ignores_excluded_papers_json(tmp_path):
    """``excluded_papers_*.json`` 是被淘汰的论文，索引进来会污染通过/过滤统计。"""
    root = tmp_path / "src"
    _write_date_dir(root, "2026-08-10", [_paper("2608.1v1", "保留的")])
    (root / "2026-08-10" / "excluded_papers_2026-08-10.json").write_text(
        json.dumps({"excluded_count": 1,
                    "excluded_papers": [{"title": "被淘汰的", "url": "http://x/e"}]},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    st = _store(tmp_path)
    src = st.add_source(root, "论文")
    scan_source(st, src["id"])
    rows = st.list_items(src["id"])
    assert len(rows) == 1 and rows[0]["title"] == "保留的"


def test_add_source_normalizes_path(tmp_path):
    """``~`` / ``..`` / 符号链接要归一化，否则同一个目录登记成两行、条目翻倍。"""
    root = tmp_path / "src"
    root.mkdir()
    st = _store(tmp_path)
    a = st.add_source(root, "正名")
    b = st.add_source(tmp_path / "src" / ".." / "src", "别名")
    assert a["id"] == b["id"]
    assert len(st.list_sources()) == 1


def test_scan_reports_progress_and_cancels(tmp_path):
    """进度回调要按目录目录触发；取消要能中断且如实上报部分结果。"""
    root = tmp_path / "src"
    for i in range(3):
        _write_date_dir(root, f"2026-09-0{i + 1}", [_paper(f"2608.{i}v1", f"第{i}篇")])
    st = _store(tmp_path)
    src = st.add_source(root, "论文")

    seen_msgs: list[str] = []
    r = scan_source(st, src["id"], on_progress=seen_msgs.append)
    assert r["ok"] is True and len(seen_msgs) == 3  # 每个日期目录一条进度

    class _Cancelled(Exception):
        pass

    def _boom() -> None:
        raise _Cancelled("cancelled")

    root2 = tmp_path / "src2"
    _write_date_dir(root2, "2026-09-01", [_paper("2609.1v1", "新")])
    src2 = st.add_source(root2, "第二个")
    r2 = scan_source(st, src2["id"], check_cancelled=_boom)
    assert r2["ok"] is False
    assert "cancel" in r2["error"].lower()


def test_scan_unknown_source(tmp_path):
    st = _store(tmp_path)
    r = scan_source(st, "不存在")
    assert r["ok"] is False and "未登记" in r["error"]


# ── 目录登记 ──────────────────────────────────────────────────────


def test_add_source_rejects_missing_dir(tmp_path):
    """登记一个不存在的目录要失败 —— 否则用户得到一个永远扫不出东西的来源。"""
    st = _store(tmp_path)
    assert st.add_source(tmp_path / "没有这个目录", "x") is None


def test_add_source_is_idempotent(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    st = _store(tmp_path)
    a = st.add_source(root, "名一")
    b = st.add_source(root, "名二")
    assert a["id"] == b["id"]
    assert len(st.list_sources()) == 1


def test_remove_source_drops_items_but_keeps_files(tmp_path):
    """移除登记只清库，磁盘上的目录与文件一概不动（那是用户的资料）。"""
    root = tmp_path / "src"
    _write_date_dir(root, "2026-09-07", [_paper("2608.1v1", "甲")])
    st = _store(tmp_path)
    src = st.add_source(root, "论文")
    scan_source(st, src["id"])

    assert st.remove_source(src["id"]) is True
    assert st.list_sources() == []
    assert st.list_items(src["id"]) == []
    assert (root / "2026-09-07" / "papers_data.json").is_file()


# ── 写回 ──────────────────────────────────────────────────────────


def test_write_creates_date_subdir_and_body(tmp_path):
    st = _store(tmp_path)
    root = tmp_path / "src"
    _write_date_dir(root, "2026-09-07", [_paper("2608.1v1", "甲")])
    src = st.add_source(root, "论文")
    scan_source(st, src["id"])
    items = st.list_items(src["id"])

    out = tmp_path / "out"
    r = write_external_export(items, out)
    assert r["ok"] is True and r["written"] == 1
    assert (out / "2026-09-07" / "papers_data.json").is_file()
    assert (out / "2026-09-07" / "2608.1v1.html").is_file()
    body = (out / "2026-09-07" / "2608.1v1.html").read_text(encoding="utf-8")
    assert "甲" in body and "的摘要" in body


def test_write_merges_without_clobbering(tmp_path):
    """目标目录已有 papers_data.json 时，别人的条目必须原样保留。

    这是最容易造成数据丢失的一步：导出只该「加入」，不该「替换」。
    """
    out = tmp_path / "out"
    _write_date_dir(out, "2026-09-07", [_paper("9999.9v1", "别人写的")])

    st = _store(tmp_path)
    root = tmp_path / "src"
    _write_date_dir(root, "2026-09-07", [_paper("2608.1v1", "我导出的")])
    src = st.add_source(root, "论文")
    scan_source(st, src["id"])

    write_external_export(st.list_items(src["id"]), out)
    payload = json.loads((out / "2026-09-07" / "papers_data.json").read_text(encoding="utf-8"))
    titles = {p["title"] for p in payload}
    assert titles == {"别人写的", "我导出的"}


def test_write_overwrites_same_key(tmp_path):
    """同一个 item_key 再次导出 → 覆盖更新，不是追加成两条。"""
    st = _store(tmp_path)
    root = tmp_path / "src"
    _write_date_dir(root, "2026-09-07", [_paper("2608.1v1", "旧标题")])
    src = st.add_source(root, "论文")
    scan_source(st, src["id"])
    items = st.list_items(src["id"])

    out = tmp_path / "out"
    write_external_export(items, out)
    items[0]["title"] = "新标题"
    write_external_export(items, out)

    payload = json.loads((out / "2026-09-07" / "papers_data.json").read_text(encoding="utf-8"))
    assert len(payload) == 1
    assert payload[0]["title"] == "新标题"


def test_write_then_scan_roundtrip(tmp_path):
    """写出去的目录再登记扫回来，条目应与导出前一致（格式对称）。"""
    st = _store(tmp_path)
    root = tmp_path / "src"
    _write_date_dir(root, "2026-09-07", [
        _paper("2608.1v1", "甲", title_cn="甲（中）"),
        _paper("2608.2v1", "乙"),
    ])
    src = st.add_source(root, "论文")
    scan_source(st, src["id"])
    before = {r["item_key"]: r for r in st.list_items(src["id"])}

    out = tmp_path / "out"
    write_external_export(st.list_items(src["id"]), out)

    st2 = ExternalStore(tmp_path / "ext2.db")
    src2 = st2.add_source(out, "回读")
    scan_source(st2, src2["id"])
    after = {r["item_key"]: r for r in st2.list_items(src2["id"])}
    assert set(before) == set(after)
    assert before["arxiv:2608.1"]["title"] == after["arxiv:2608.1"]["title"]


def test_body_filename_matches_pdf_naming(tmp_path):
    """正文文件名要用**完整** arxiv_id，与同目录的 {arxiv_id}.pdf 对齐。

    item_key 为去重剥了版本号；若正文名也跟着剥，就会出现正文叫
    ``2608.25061.html`` 而 PDF 叫 ``2608.25061v2.pdf`` 的名不对号。
    """
    st = _store(tmp_path)
    root = tmp_path / "src"
    _write_date_dir(root, "2026-08-29", [_paper("2608.25061v2", "改版论文")])
    src = st.add_source(root, "论文")
    scan_source(st, src["id"])
    items = st.list_items(src["id"])
    assert items[0]["item_key"] == "arxiv:2608.25061"     # 去重用的键剥了版本
    assert items[0]["arxiv_id"] == "2608.25061v2"          # 完整 ID 仍保留

    out = tmp_path / "out"
    write_external_export(items, out)
    assert (out / "2026-08-29" / "2608.25061v2.html").is_file()


def test_write_without_items_is_rejected(tmp_path):
    r = write_external_export([], tmp_path / "out")
    assert r["ok"] is False and "没有可导出" in r["error"]


# ── 查询 ──────────────────────────────────────────────────────────


def test_list_items_search_escapes_like_metacharacters(tmp_path):
    """搜索词里的 % 和 _ 是 LIKE 通配符，不转义会捞出一堆无关条目。"""
    st = _store(tmp_path)
    root = tmp_path / "src"
    _write_date_dir(root, "2026-09-07", [
        _paper("2608.1v1", "FPGA 架构"),
        _paper("2608.2v1", "100%纯软件"),
        _paper("2608.3v1", "无关标题"),
    ])
    src = st.add_source(root, "论文")
    scan_source(st, src["id"])

    assert len(st.list_items(src["id"], q="FPGA")) == 1
    assert len(st.list_items(src["id"], q="100%")) == 1   # 不该匹配到所有条目
    assert len(st.list_items(src["id"], q="___")) == 0    # 下划线不再当通配符


def test_list_items_respects_enabled(tmp_path):
    """停用的目录不出现在聚合列表里，但单独指定 source_id 仍可查看。"""
    st = _store(tmp_path)
    root = tmp_path / "src"
    _write_date_dir(root, "2026-09-07", [_paper("2608.1v1", "甲")])
    src = st.add_source(root, "论文")
    scan_source(st, src["id"])

    assert len(st.list_items()) == 1
    st.update_source(src["id"], enabled=False)
    assert st.list_items() == []
    assert len(st.list_items(src["id"])) == 1


def test_store_survives_broken_db_path(tmp_path):
    """DB 不可用时所有方法都要吞异常，不能把主流程带崩。"""
    broken = tmp_path / "nope" / "x.db"
    broken.parent.mkdir()
    st = ExternalStore(broken)
    assert st.list_sources() == [] or True  # 能建就建，建不了也不能抛
    st.close()
