"""打包清单的漂移守卫（2026-09，做 Windows 版时把两份 spec 的清单合并后加）。

**这个仓库真栽过。** v2.0.4 / v2.0.5 的 mac spec 漏打了 ``core/templates``，结果
**导出功能全部失败** —— 而且因为找不到模板时是安静回退，用户只看到「导出失败」，
查了很久。两份 spec 各写一遍 ``datas`` 必然重演，所以清单统一在
``packaging/build_common.py``，这里负责把「清单」与「运行时代码真正探测的路径」
对着钉一遍。

不只比对字符串：下面几条**真的模拟冻结版的目录布局**，再调用运行时那个探测函数，
所以它验的是契约本身，而不是我抄得对不对。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_REPO))

PKG_ROOT = ROOT_REPO / "mp_harvest"
PACKAGING = PKG_ROOT / "packaging"
sys.path.insert(0, str(PACKAGING))

import build_common  # noqa: E402

from mp_harvest.core import article_reader, weekly_report  # noqa: E402
from mp_harvest.infra.platform import paths as paths_mod  # noqa: E402


# ── 清单本身 ─────────────────────────────────────────────────────


def test_required_resources_are_in_the_manifest():
    """三个目标路径缺一不可，且**字面量**不能变（运行时按字面量探测）。"""
    dests = {dest for _, dest in build_common.package_datas(PKG_ROOT)}
    assert "mp_harvest/core/templates" in dests, "少了模板 → 导出功能全挂（v2.0.4 事故）"
    assert "frontend/dist" in dests, "少了前端产物 → 开窗是空白页"
    assert "frontend/public" in dests, "少了图标"


def test_template_sources_contain_the_templates():
    """源目录里必须真有代码要的那两个模板文件。

    光有目录是不够的 —— 空目录打进去，表现与漏打一模一样。
    """
    tpl = PKG_ROOT / "core" / "templates"
    assert (tpl / "article.html").is_file()
    assert (tpl / weekly_report.BUILTIN_TEMPLATE_NAME).is_file()


def test_manifest_sources_exist():
    """清单里的源路径都要真的存在（PyInstaller 对缺失的 datas 只发警告！）。

    前端产物在没跑过 ``npm run build`` 的检出里本来就没有，那种情况跳过它。
    """
    built = (PKG_ROOT / "frontend" / "dist").is_dir()
    missing = []
    for src, _dest in build_common.package_datas(PKG_ROOT):
        if not built and "frontend" in Path(src).parts:
            continue
        if not Path(src).exists():
            missing.append(src)
    assert not missing, f"打包清单里的源路径不存在：{missing}"


def test_platform_specific_hiddenimports_do_not_cross():
    """``_scproxy`` 是 macOS 独占的 C 扩展，**绝不能**带进 Windows 包。"""
    assert "_scproxy" in build_common.MAC_HIDDENIMPORTS
    assert "_scproxy" not in build_common.WIN_HIDDENIMPORTS
    # EdgeChromium 后端靠 pythonnet/clr；漏了会退回 IE11 → 纯白窗口，没有任何报错
    assert "clr" in build_common.WIN_HIDDENIMPORTS
    assert "clr" not in build_common.MAC_HIDDENIMPORTS


def test_version_is_single_sourced():
    """两份 spec 都从 base.py 取版本号，不各解析一遍。"""
    from mp_harvest.infra.platform.base import APP_VERSION

    assert build_common.version_from_source(PKG_ROOT) == APP_VERSION


def test_windows_version_resource_uses_app_version(tmp_path):
    from mp_harvest.infra.platform.base import APP_VERSION

    path = Path(build_common.windows_version_file(PKG_ROOT, tmp_path))
    text = path.read_text(encoding="utf-8")
    assert f"StringStruct('FileVersion', '{APP_VERSION}')" in text
    assert "VSVersionInfo(" in text


# ── 真的按冻结版布局摆一遍，再调运行时探测 ───────────────────────


@pytest.fixture()
def frozen_layout(tmp_path, monkeypatch):
    """把 ``datas`` 按清单铺成一个假的 ``_MEIPASS``，并把 sys._MEIPASS 指过去。"""
    root = tmp_path / "_internal"
    root.mkdir()
    for src, dest in build_common.package_datas(PKG_ROOT):
        src_path = Path(src)
        if not src_path.exists():
            continue
        target = root / dest
        if src_path.is_dir():
            shutil.copytree(src_path, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, target)
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)
    monkeypatch.setattr(paths_mod, "is_frozen", lambda: True)
    return root


def test_article_export_finds_templates_in_frozen_layout(frozen_layout):
    """导出模板探测（``article_reader._resolve_template_dir``）。"""
    got = article_reader._resolve_template_dir()
    assert got == frozen_layout / "mp_harvest" / "core" / "templates", got
    assert (got / "article.html").is_file()


def test_weekly_report_finds_builtin_template_in_frozen_layout(frozen_layout):
    """周报内置模板探测（``weekly_report.resolve_template_dir``）。"""
    got = weekly_report.resolve_template_dir()
    assert got == frozen_layout / "mp_harvest" / "core" / "templates", got
    assert (got / weekly_report.BUILTIN_TEMPLATE_NAME).is_file()


@pytest.mark.skipif(
    not (PKG_ROOT / "frontend" / "dist").is_dir(), reason="前端未构建（npm run build）"
)
def test_package_root_finds_frontend_dist_in_frozen_layout(frozen_layout):
    """``paths.package_root()`` 必须能从 ``_MEIPASS`` 找到前端产物。

    这条正是 Windows onedir 的形状：``_MEIPASS`` 指向 ``<安装目录>/_internal``，
    而 ``frontend/dist`` 就在它下面（PyInstaller 6 的 contents_directory 默认值）。
    """
    assert paths_mod.package_root() == frozen_layout
    assert (paths_mod.package_root() / "frontend" / "dist" / "index.html").is_file()


# ── 两份 spec 都还在用这份清单 ───────────────────────────────────


@pytest.mark.parametrize("spec_name", ["MP_Harvest.spec", "MP_Harvest_win.spec"])
def test_specs_use_the_shared_manifest(spec_name):
    """spec 不能再自己写一份 datas —— 那正是漂移的起点。"""
    text = (PACKAGING / spec_name).read_text(encoding="utf-8")
    assert "package_datas(" in text, f"{spec_name} 没有用共享清单"
    assert "build_common" in text
    compile(text, spec_name, "exec")  # 至少保证语法能过


def test_mac_spec_keeps_frameworks_destination():
    """mac 的 .app 布局用不到 contents_directory，保持原样。"""
    text = (PACKAGING / "MP_Harvest.spec").read_text(encoding="utf-8")
    assert "BUNDLE(" in text
    assert "contents_directory" not in text


# ── Windows 图标 ─────────────────────────────────────────────────


def test_windows_spec_points_at_the_ico():
    text = (PACKAGING / "MP_Harvest_win.spec").read_text(encoding="utf-8")
    assert 'mp_harvest.ico' in text
    assert (PACKAGING / "mp_harvest.ico").is_file(), "跑 make_ico.py 生成它"


def test_ico_is_well_formed():
    """`.ico` 的结构自己解析一遍，并确认档数与生成脚本一致。

    本机装不了 PyInstaller 的 Windows 图标解析器（要 pywin32），所以只能验结构；
    真机上 PyInstaller 会不会挑刺，要等 Windows CI 那次构建才知道。
    """
    import make_ico  # noqa: E402  (packaging 目录已加进 sys.path)

    entries = make_ico.read_ico(PACKAGING / "mp_harvest.ico")
    assert [w for w, _h, _n in entries] == list(make_ico.SIZES)
    assert all(h == w for w, h, _n in entries)
    # 256 那档必须在不含 256 的 ICONDIRENTRY 里写成 0（一个字节放不下 256）
    data = (PACKAGING / "mp_harvest.ico").read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00"  # reserved=0, type=1(icon), count 在前 6 字节
