"""历史文章列表的列对齐（2026-09 用户报的「表头和数据不匹配」）。

症状：表头上的「标题 / AI 理由 / 时间」和下面的数据**整体错位**，看着像数据串行了。
根因有两个，都在 CSS 网格的列数上：

1. ``grid-template-columns`` 最后一列是 ``auto``。表头那一格是**空的**（0px），
   数据行那一格装着四个按钮（实测 166px）—— 于是两边的 fr 列分到的宽度不一样
   （表头 251/431/251，数据行 207/354/207），中间几列全部对不上。
2. 数据行的子元素个数**随数据变**：有导出文件的行多一个「已导出」徽标。
   列数是固定的 7，多出来的第 8 个子元素被挤到下一行（行高固定 36px + 裁剪），
   那一行的按钮就消失了。

浏览器实测确认过（修好前后各测一遍）：修好后表头与每一行的 8 个格子
x 坐标逐一对齐（229/273/466/790/983/1059/1127/1195）。

⚠️ 这组是**静态检查**，不是渲染断言 —— 它盯的是那两个机制（``auto`` 末列、
子元素个数可变），不是真的去量像素。真量像素要开浏览器，进不了 pytest。
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "mp_harvest" / "frontend"
HISTORY_VIEW = FRONTEND / "src" / "views" / "HistoryView.vue"
STYLE = FRONTEND / "src" / "style.css"

# `.art-head,.art-row{display:grid;grid-template-columns:...}`
_GRID_RULE = re.compile(
    r"\.art-head,\s*\.art-row\s*\{[^}]*grid-template-columns:\s*([^;}]+)"
)
# 模板里的表头那一行
_HEAD_ROW = re.compile(r'<div class="art-head">(.*?)</div>', re.S)


def _split_columns(value: str) -> list[str]:
    """按**括号外的空白**切开列定义。

    不能直接 ``value.split()``：``minmax(90px,.7fr)`` 里面有空格和逗号，
    那样会把一个 minmax 拆成好几段、数出来的列数是错的（列数是这个测试的
    全部意义，数错了测试就变成空转）。用一个小状态机按括号深度切。
    """
    out: list[str] = []
    depth = 0
    cur = ""
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch.isspace() and depth == 0:
            if cur:
                out.append(cur)
                cur = ""
            continue
        cur += ch
    if cur:
        out.append(cur)
    return out


def _grid_columns() -> list[str]:
    m = _GRID_RULE.search(STYLE.read_text(encoding="utf-8"))
    assert m, "style.css 里找不到 .art-head,.art-row 的 grid-template-columns"
    return _split_columns(m.group(1))


def test_last_grid_column_is_not_auto():
    """末列必须是固定宽度 —— ``auto`` 会让表头（空格子）和数据行（按钮）算出不同列宽。

    这是错位的**根因**：只要末列是 ``auto``，表头和每一行的 fr 分配就永远可能
    不一致，而且不一致的程度取决于按钮文字有多宽（改个按钮文案就会变）。
    """
    cols = _grid_columns()
    assert cols[-1] != "auto", (
        f"末列是 auto，表头与数据行会各自算列宽 → 整体错位。当前：{cols}"
    )
    assert "auto" not in cols, f"列定义里不该出现 auto：{cols}"


def test_header_has_exactly_as_many_cells_as_columns():
    """表头的格子数 == 列数。少一格 = 从那一格开始整体左移一格。"""
    cols = _grid_columns()
    m = _HEAD_ROW.search(HISTORY_VIEW.read_text(encoding="utf-8"))
    assert m, "HistoryView.vue 里找不到 .art-head"
    cells = len(re.findall(r"<span", m.group(1)))
    assert cells == len(cols), (
        f"表头 {cells} 格 vs 网格 {len(cols)} 列（{cols}）—— 对不上就会整体错位"
    )


def test_row_children_count_does_not_depend_on_flags():
    """数据行里带 ``v-if`` 的格子必须有 ``v-else`` 占位。

    行的子元素个数一旦随数据变（比如只有导出的文章才多一个徽标），多出来的
    子元素就会被挤到下一行 —— 行高固定 36px，那一行的按钮直接被裁掉。
    """
    src = HISTORY_VIEW.read_text(encoding="utf-8")
    # 数据行两处（直接渲染 + 虚拟滚动）。按**行自己的结束标签**（10 空格缩进）
    # 切片 —— 用「往后找 400 字符看有没有 v-else」那种松办法不行：两个行模板
    # 挨在一起，第一个行里缺的 v-else 会被第二个行的 v-else「顶包」，
    # 实测那种写法让「删掉占位」的变异体活了下来。
    rows = re.findall(r'class="art-row"(.*?)\n          </div>', src, re.S)
    assert len(rows) == 2, f"应当有 2 个数据行模板（直接渲染 + 虚拟滚动），实际 {len(rows)}"
    offenders: list[str] = []
    for chunk in rows:
        for m in re.finditer(r"<(SBadge|span|STooltip)\b[^>]*v-if=", chunk):
            # 占位必须是**紧跟其后的那个兄弟元素**。早先写成「本行后面出现过
            # v-else 就行」，结果同一个行里别的格子的 v-else 会把它顶包 ——
            # 删掉 AI 理由格的占位照样绿（变异测试抓出来的）。
            close = f"</{m.group(1)}>"
            end = chunk.find(close, m.end())
            # 从**这个元素的结束标签之后**找下一个元素。不能只看「下一行」：
            # <STooltip> 这种开标签独占一行、内容在下一行，下一行是它的**内部**，
            # 不是兄弟（实测这么写会把正确的代码判成不合格）。
            rest = chunk[end + len(close):] if end != -1 else chunk[m.end():]
            nxt = next((ln.strip() for ln in rest.splitlines() if ln.strip()), "")
            if not (nxt.startswith("<") and "v-else" in nxt):
                offenders.append(
                    (chunk[m.start(): m.end() + 40] + " ← 下一个元素是：" + (nxt[:60] or "(行尾)")).replace("\n", " ")
                )
    assert not offenders, (
        "这些带 v-if 的格子在条件不成立时**不留占位**，行会少一格 → 后面整体前移：\n"
        + "\n".join(offenders)
    )


def test_header_lives_inside_the_scroll_container():
    """表头必须在滚动容器**内部**。

    放在外面时，行数一多就会出现纵向滚动条，滚动区比 .art-table 窄（实测
    macOS 上差 8px），而表头仍按 .art-table 的宽度算列 —— 整列向右漂移最多
    8px，越靠右越明显。症状跟「末列 auto」一模一样（都是列宽算不一样），
    但机制不同，得单独钉一条。（滚动条只在大列表出现，实测 7 行时不偏、
    527 行时偏 8px —— 只测小列表会漏掉。）
    """
    src = HISTORY_VIEW.read_text(encoding="utf-8")
    scroll_at = src.find('class="art-scroll"')
    head_at = src.find('class="art-head"')
    assert scroll_at != -1 and head_at != -1, "找不到 .art-scroll / .art-head"
    assert head_at > scroll_at, (
        "表头在滚动容器外面 —— 出现滚动条时表头会比数据行宽，列会错位"
    )
    # 而且必须吸顶，否则滚一下表头就没了
    css = STYLE.read_text(encoding="utf-8")
    m = re.search(r"\.art-head\{([^}]*)\}", css)
    assert m and "sticky" in m.group(1), ".art-head 没有 position:sticky（表头要吸顶）"
