"""生成应用图标。

用 Pillow 直接绘制而不是渲染 SVG：macOS 上没有可靠的命令行 SVG 渲染器，
而图标形状都是圆角矩形、圆、多边形，直接画反而更可控。

统一在 4× 尺寸下绘制再降采样（LANCZOS），边缘比直接画小图干净得多。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

S = 2048                      # 绘制尺寸；设计坐标以 512 为准，统一乘 SCALE
SCALE = S / 512
BRAND_TOP = (23, 104, 255)    # #1768ff
BRAND_BOTTOM = (85, 167, 255)  # #55a7ff
INK = (255, 255, 255, 255)
ACCENT = (40, 120, 255, 255)


def u(value: float) -> float:
    """设计坐标（512 基准）→ 绘制坐标。"""
    return value * SCALE


def diagonal_gradient(size: int, start: tuple[int, int, int], end: tuple[int, int, int]) -> Image.Image:
    """135° 线性渐变。先在低分辨率算准，再放大 —— 平滑渐变经得起插值。"""
    small = 64
    base = Image.new("RGB", (small, small))
    pixels = base.load()
    for y in range(small):
        for x in range(small):
            t = (x + y) / (2 * (small - 1))
            pixels[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(start, end))
    return base.resize((size, size), Image.Resampling.BICUBIC)


def squircle_mask(size: int, radius: float) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def base_canvas() -> Image.Image:
    """渐变底 + 顶部高光，再切圆角。"""
    canvas = diagonal_gradient(S, BRAND_TOP, BRAND_BOTTOM).convert("RGBA")
    sheen = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(sheen)
    for row in range(int(u(230))):
        alpha = int(56 * (1 - row / u(230)) ** 1.6)
        draw.line([(0, row), (S, row)], fill=(255, 255, 255, alpha))
    canvas = Image.alpha_composite(canvas, sheen)
    canvas.putalpha(squircle_mask(S, u(114)))
    return canvas


def _layer():
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    return layer, ImageDraw.Draw(layer)


def _bar(draw, x, y, w, h, alpha=255, fill=None):
    draw.rounded_rectangle(
        (u(x), u(y), u(x + w), u(y + h)), radius=u(h / 2),
        fill=fill or (255, 255, 255, alpha),
    )


# ---------------------------------------------------------------- A 时间地层
def concept_a(canvas: Image.Image) -> Image.Image:
    layer, draw = _layer()
    _bar(draw, 176, 150, 160, 46, alpha=133)
    _bar(draw, 134, 222, 244, 46, alpha=199)
    _bar(draw, 92, 294, 328, 46, alpha=255)
    return Image.alpha_composite(canvas, layer)


# ------------------------------------------------------- A+ 地层（顶层流失）
def concept_a_plus(canvas: Image.Image) -> Image.Image:
    """A 的构图 + B 的概念：最上层断成碎片，暗示正在流失。"""
    layer, draw = _layer()
    for x, w, alpha in ((176, 44, 95), (236, 40, 120), (292, 44, 150)):
        _bar(draw, x, 150, w, 46, alpha=alpha)
    _bar(draw, 134, 222, 244, 46, alpha=210)
    _bar(draw, 92, 294, 328, 46, alpha=255)
    return Image.alpha_composite(canvas, layer)


# ---------------------------------------------------------------- B 云散归岸
def concept_b(canvas: Image.Image, *, bold: bool = False) -> Image.Image:
    """上方消散的云弧 + 下行箭头 + 厚实地基。

    ``bold`` 是给 16/32px 用的字形微调：那两档下细弧会被抹平、细节糊成一团，
    所以加粗弧线、放大箭头、收紧留白。各档单独调优是图标设计的常规做法。
    """
    layer, draw = _layer()
    if bold:
        arc_width, arc_alpha = int(u(40)), 185
        arc_box = (u(140), u(104), u(372), u(250))
        segments = ((192, 240), (252, 300), (312, 356))
        stem = (230, 200, 52, 96)
        head = ((256, 360), (176, 268), (336, 268))
        base = (88, 384, 336, 56)
    else:
        arc_width, arc_alpha = int(u(26)), 150
        arc_box = (u(150), u(96), u(362), u(252))
        segments = ((188, 232), (244, 290), (302, 352))
        stem = (238, 196, 36, 104)
        head = ((256, 352), (192, 272), (320, 272))
        base = (104, 378, 304, 46)

    for start, end in segments:
        draw.arc(arc_box, start=start, end=end, fill=(255, 255, 255, arc_alpha), width=arc_width)
    _bar(draw, *stem)
    draw.polygon([(u(x), u(y)) for x, y in head], fill=INK)
    _bar(draw, *base)
    return Image.alpha_composite(canvas, layer)


# ------------------------------------------------------------------ C 封存箱
def concept_c(canvas: Image.Image) -> Image.Image:
    layer, draw = _layer()
    # 正在落入的纸页
    draw.rounded_rectangle((u(200), u(92), u(312), u(196)), radius=u(16), fill=(255, 255, 255, 140))
    draw.polygon([(u(256), u(222)), (u(214), u(176)), (u(298), u(176))], fill=(255, 255, 255, 190))
    # 箱体
    draw.rounded_rectangle((u(100), u(236), u(412), u(420)), radius=u(26), fill=INK)
    # 封条
    draw.rounded_rectangle((u(210), u(264), u(302), u(296)), radius=u(16), fill=ACCENT)
    return Image.alpha_composite(canvas, layer)


# ---------------------------------------------------------------- D 时光胶囊
def concept_d(canvas: Image.Image) -> Image.Image:
    layer, draw = _layer()
    draw.ellipse((u(130), u(130), u(382), u(382)), fill=(255, 255, 255, 40))
    draw.ellipse((u(130), u(130), u(382), u(382)), outline=INK, width=int(u(22)))
    # 文档
    draw.rounded_rectangle((u(212), u(196), u(300), u(308)), radius=u(14), fill=INK)
    for y, w in ((218, 56), (244, 56), (270, 36)):
        draw.rounded_rectangle((u(228), u(y), u(228 + w), u(y + 12)), radius=u(6), fill=ACCENT)
    # 四个锁闩
    for x0, y0, x1, y1 in ((244, 86, 268, 128), (244, 384, 268, 426),
                           (86, 244, 128, 268), (384, 244, 426, 268)):
        draw.rounded_rectangle((u(x0), u(y0), u(x1), u(y1)), radius=u(12), fill=(255, 255, 255, 235))
    return Image.alpha_composite(canvas, layer)


CONCEPTS = {
    "A": ("时间地层", concept_a),
    "A+": ("时间地层·顶层流失", concept_a_plus),
    "B": ("云散归岸", concept_b),
    "C": ("封存箱", concept_c),
    "D": ("时光胶囊", concept_d),
}

SIZES = (512, 256, 128, 64, 32, 16)


BOLD_BELOW = 48          # 这个尺寸以下改用加粗字形


def render(name: str, *, bold: bool = False) -> Image.Image:
    builder = CONCEPTS[name][1]
    if bold and name == "B":
        return builder(base_canvas(), bold=True)
    return builder(base_canvas())


def render_at(name: str, size: int) -> Image.Image:
    master = render(name, bold=size < BOLD_BELOW)
    return master.resize((size, size), Image.Resampling.LANCZOS)


def write_set(name: str, out_dir: Path) -> list[Path]:
    folder = out_dir / name.replace("+", "plus")
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for size in (*SIZES, 1024):
        path = folder / f"icon_{size}.png"
        render_at(name, size).save(path)
        written.append(path)
    return written


def _font(size: int):
    """系统中文字体。Pillow 自带字体没有 CJK 字形，标签会变成方块。"""
    for candidate in ("/System/Library/Fonts/PingFang.ttc",
                      "/System/Library/Fonts/Hiragino Sans GB.ttc",
                      "/System/Library/Fonts/STHeiti Light.ttc"):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def contact_sheet(out: Path) -> Path:
    """对照表：每行一个方案，依次 256/128/64/32/16，附深色背景条。"""
    pad, gap = 40, 26
    row_h = 256 + 44
    width = pad * 2 + 256 + gap + 128 + gap + 64 + gap + 32 + gap + 16 + 260
    height = pad * 2 + row_h * len(CONCEPTS)
    sheet = Image.new("RGB", (width, height), (243, 246, 251))
    draw = ImageDraw.Draw(sheet)

    y = pad + 10
    for key, (label, _) in CONCEPTS.items():
        x = pad
        for size in (256, 128, 64, 32, 16):
            thumb = render_at(key, size)
            sheet.paste(thumb, (x, y + 256 - size), thumb)
            draw.text((x, y + 264), str(size), fill=(105, 115, 134), font=_font(13))
            x += size + gap
        # 深色条，检查在暗色 Dock 上的表现
        dark_x = width - pad - 240
        draw.rounded_rectangle((dark_x, y + 150, dark_x + 240, y + 256), radius=18, fill=(15, 20, 29))
        for i, size in enumerate((64, 48, 32)):
            thumb = render_at(key, size)
            sheet.paste(thumb, (dark_x + 20 + i * 78, y + 203 - size // 2), thumb)
        draw.text((pad, y - 26), f"{key}   {label}", fill=(23, 32, 51), font=_font(19))
        y += row_h

    sheet.save(out)
    return out


# ---------------------------------------------------------------- 发布产物
SHIPPING = "B"               # 选定方案：云散归岸

# Apple 要求的 iconset 命名。@2x 用两倍像素尺寸渲染，不是放大。
ICONSET = (
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
)


def build_shipping_icons(assets: Path) -> dict[str, Path]:
    """生成 icon.png / icon.icns / icon.ico。"""
    import shutil
    import subprocess

    assets.mkdir(parents=True, exist_ok=True)
    produced: dict[str, Path] = {}

    png = assets / "icon.png"
    render_at(SHIPPING, 1024).save(png)
    produced["png"] = png

    iconset = assets / "icon.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir()
    for name, size in ICONSET:
        render_at(SHIPPING, size).save(iconset / name)

    if shutil.which("iconutil"):
        icns = assets / "icon.icns"
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
        produced["icns"] = icns
        shutil.rmtree(iconset, ignore_errors=True)   # icns 已生成，中间目录不必留

    ico = assets / "icon.ico"
    render_at(SHIPPING, 256).save(
        ico, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
    )
    produced["ico"] = ico
    return produced


if __name__ == "__main__":
    if "--concepts" in sys.argv:
        argv = [a for a in sys.argv[1:] if not a.startswith("--")]
        target = Path(argv[0]) if argv else Path("build/icons")
        target.mkdir(parents=True, exist_ok=True)
        for key in CONCEPTS:
            write_set(key, target)
        print(contact_sheet(target / "contact-sheet.png"))
    else:
        icon_dir = Path(__file__).resolve().parents[1] / "baidu_archive" / "ui"
        for kind, path in build_shipping_icons(icon_dir).items():
            print(f"{kind:5} {path}  ({path.stat().st_size / 1024:.0f} KB)")
