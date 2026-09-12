#!/usr/bin/env python3
"""从 ``mp_harvest.icns`` 生成 Windows 用的 ``mp_harvest.ico``（2026-09）。

**为什么要有这个脚本。** 仓库里只有 mac 的 ``.icns``，而 PyInstaller 在 Windows 上
要 ``.ico``。实测 ``sips`` **不能**写 ICO（``-s format ico`` 退出码 0 但不产出文件），
所以走「sips 出 BMP → 自己拼 ICONDIR」这条路，**纯标准库**，不引入 Pillow。

生成一次后 ``.ico`` 就**提交进仓库**（与 ``.icns`` 同等待遇），构建过程不依赖本脚本，
也不依赖 Pillow 或 macOS —— 这个脚本只在需要改图标时手动跑一次：

    python3 mp_harvest/packaging/make_ico.py

产物用两个独立的解码器验证：脚本自带的解析回读，以及 macOS 的 ImageIO
（``sips -g pixelWidth``）。
"""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ICNS = HERE / "mp_harvest.icns"
ICO = HERE / "mp_harvest.ico"

# Windows 会按 DPI/场景挑最合适的一档；256 是任务栏与「大图标」视图用的
SIZES = (16, 24, 32, 48, 64, 128, 256)


def largest_png_from_icns(path: Path) -> bytes:
    """从 .icns 里取像素最多的那张 PNG。

    icns 就是一个「类型 + 长度 + 数据」的块序列，现代图标里各档都是 PNG。
    """
    data = path.read_bytes()
    if data[:4] != b"icns":
        raise SystemExit(f"{path} 不是 icns 文件")
    best: tuple[int, bytes] = (0, b"")
    off = 8
    while off + 8 <= len(data):
        length = struct.unpack(">I", data[off + 4 : off + 8])[0]
        if length < 8 or off + length > len(data):
            break
        payload = data[off + 8 : off + length]
        if payload[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", payload[16:24])
            if w * h > best[0]:
                best = (w * h, payload)
        off += length
    if not best[1]:
        raise SystemExit(f"{path} 里没有找到 PNG 档")
    return best[1]


def bmp_to_ico_entry(bmp: bytes) -> tuple[int, int, bytes]:
    """把一个 32 位 BMP 转成 ICO 里的一条 DIB 条目，返回 ``(宽, 高, 数据)``。

    ICO 的条目与 BMP 有两处不同：

    1. ``biHeight`` 是**两倍**图高 —— 图像数据之后还跟着一张 1bpp 的 AND 掩码；
    2. 像素行是**自下而上**的，而 sips 给的是自上而下（负高度）。

    32bpp 的 BITMAPINFOHEADER 已经是 4 字节对齐，不需要按行补位；AND 掩码按惯例
    全填 0（透明度由 alpha 通道表达，掩码只是给不支持 alpha 的老程序兜底）。
    """
    if bmp[:2] != b"BM":
        raise SystemExit("不是 BMP 数据")
    pix_off = struct.unpack("<I", bmp[10:14])[0]
    hdr_size = struct.unpack("<I", bmp[14:18])[0]
    width, height = struct.unpack("<ii", bmp[18:26])
    bpp = struct.unpack("<H", bmp[28:30])[0]
    if bpp != 32:
        raise SystemExit(f"只处理 32 位 BMP，收到 {bpp} 位")
    top_down = height < 0
    height = abs(height)

    header = struct.pack(
        "<IiiHHIIiiII",
        hdr_size,          # biSize
        width,             # biWidth
        height * 2,        # biHeight：XOR 图 + AND 掩码
        1,                 # biPlanes
        32,                # biBitCount
        0,                 # biCompression = BI_RGB
        width * height * 4,
        0, 0, 0, 0,
    )
    body = bmp[pix_off : pix_off + width * height * 4]
    if len(body) < width * height * 4:
        raise SystemExit("BMP 像素数据长度不对")

    stride = width * 4
    if top_down:
        rows = [body[y * stride : (y + 1) * stride] for y in range(height)]
        body = b"".join(reversed(rows))
    mask_stride = ((width + 31) // 32) * 4
    return width, height, header + body + b"\x00" * (mask_stride * height)


def read_ico(path: Path) -> list[tuple[int, int, int]]:
    """解析 ICO，返回 ``[(宽, 高, 数据字节数), ...]`` —— 验证用。"""
    data = path.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    if reserved != 0 or kind != 1:
        raise SystemExit("ICO 头不对")
    out = []
    for i in range(count):
        w, h, _colors, _rsv, _planes, _bpp, size, offset = struct.unpack(
            "<BBBBHHII", data[6 + i * 16 : 22 + i * 16]
        )
        out.append((w or 256, h or 256, size))
        if offset + size > len(data):
            raise SystemExit(f"第 {i} 条超出文件末尾")
    return out


def build(icns: Path = ICNS, ico: Path = ICO) -> Path:
    png = largest_png_from_icns(icns)
    entries: list[tuple[int, int, bytes]] = []
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src.png"
        src.write_bytes(png)
        for size in SIZES:
            bmp = Path(td) / f"{size}.bmp"
            subprocess.run(
                ["sips", "-s", "format", "bmp", "-Z", str(size), str(src), "--out", str(bmp)],
                check=True,
                capture_output=True,
            )
            entries.append(bmp_to_ico_entry(bmp.read_bytes()))

    header = struct.pack("<HHH", 0, 1, len(entries))
    directory = b""
    offset = len(header) + 16 * len(entries)
    blob = b""
    for width, height, payload in entries:
        directory += struct.pack(
            "<BBBBHHII",
            width if width < 256 else 0,
            height if height < 256 else 0,
            0, 0, 1, 32, len(payload), offset + len(blob),
        )
        blob += payload
    ico.write_bytes(header + directory + blob)
    return ico


def main() -> int:
    out = build()
    parsed = read_ico(out)
    print(f"已生成 {out}（{out.stat().st_size} 字节）")
    for w, h, size in parsed:
        print(f"  {w}x{h}  {size} 字节")
    # 独立解码器复核：macOS 的 ImageIO 能读 ICO，读得动说明结构没写坏
    proc = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(out)],
        capture_output=True,
        text=True,
    )
    print(proc.stdout.strip() or proc.stderr.strip())
    if proc.returncode != 0:
        print("⚠️ 系统解码器读不了这个 .ico，结构可能有问题")
        return 1
    if len(parsed) != len(SIZES):
        print(f"⚠️ 期望 {len(SIZES)} 档，实际 {len(parsed)} 档")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
