"""生成应用图标 assets/ai-gateway.ico（构建期工具，不是运行时依赖）。

## 图标意象

「多路汇聚过网关」—— 三个不同颜色的入口点（代表多家 provider）
用细线汇聚到中心发光的节点（网关），再由中心向右侧送出一路。
这是这个项目真正在做的事，不是随便画的装饰。

## 为什么先画 1024 再缩

小尺寸图标（16×16）如果直接从大图缩放会糊。Pillow 生成 ico 时
自己会按 sizes 列表逐尺寸缩放，用 LANCZOS 保持锐利。

用法：python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024                       # 主画布尺寸
BG_TOP = (24, 26, 33)          # 深色渐变（上）
BG_BOT = (14, 15, 19)          # 深色渐变（下）
ACCENT = (78, 201, 176)        # 青绿 —— 和 GUI 的 modeAuto 同色
NODE = (232, 236, 245)         # 中心节点
IN_COLORS = [(78, 201, 176), (220, 182, 122), (96, 160, 220)]   # 三路入口
OUT_COLOR = (126, 200, 255)


def _rounded_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1],
                                        radius=radius, fill=255)
    return m


def build(size: int = S) -> Image.Image:
    # ① 深色渐变底
    bg = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(bg)
    for y in range(size):
        t = y / (size - 1)
        d.line([(0, y), (size, y)],
               fill=tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOT)))
    img = bg.convert("RGBA")

    # ② 细边框（深色底上的一圈浅描边，缩到 16px 也不消失）
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, size - 3, size - 3], radius=int(size * 0.22),
                        outline=(58, 60, 72), width=max(2, size // 180))

    # ③ 汇聚线：三个入口 → 中心节点 → 右侧出口
    cx, cy = int(size * 0.52), int(size * 0.5)
    r_node = int(size * 0.115)
    entries = [
        (int(size * 0.155), int(size * 0.235)),   # 左上
        (int(size * 0.115), int(size * 0.775)),   # 左下
        (int(size * 0.175), int(size * 0.5)),     # 正左
    ]
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    lw = max(3, size // 90)
    for (ex, ey), color in zip(entries, IN_COLORS):
        gd.line([(ex, ey), (cx, cy)], fill=(*color, 210), width=lw)
    gd.line([(cx + r_node, cy), (int(size * 0.86), cy)],
            fill=(*OUT_COLOR, 220), width=lw)

    # 线条外发光（先画粗一层再模糊，叠上去）
    halo = glow.filter(ImageFilter.GaussianBlur(size // 90))
    img = Image.alpha_composite(img, halo)
    img = Image.alpha_composite(img, glow)

    # ④ 入口点（实心圆 + 深色内环，小尺寸时有“点”的辨识度）
    d = ImageDraw.Draw(img)
    r_dot = int(size * 0.052)
    for (ex, ey), color in zip(entries, IN_COLORS):
        d.ellipse([ex - r_dot, ey - r_dot, ex + r_dot, ey + r_dot], fill=color)

    # ⑤ 中心节点：光晕 + 实心 + 青绿内芯
    nglow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(nglow).ellipse(
        [cx - r_node * 2, cy - r_node * 2, cx + r_node * 2, cy + r_node * 2],
        fill=(*ACCENT, 70))
    img = Image.alpha_composite(img, nglow.filter(
        ImageFilter.GaussianBlur(size // 40)))
    d = ImageDraw.Draw(img)
    d.ellipse([cx - r_node, cy - r_node, cx + r_node, cy + r_node], fill=NODE)
    r_core = int(r_node * 0.52)
    d.ellipse([cx - r_core, cy - r_core, cx + r_core, cy + r_core], fill=ACCENT)

    # ⑥ 圆角裁切（只保留圆角方块内的内容）
    img.putalpha(_rounded_mask(size, int(size * 0.22)))
    return img


def main() -> None:
    out_dir = Path(__file__).resolve().parents[1] / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    icon = build(S)
    # PNG 预览（方便肉眼检查/README 用）
    icon.resize((256, 256), Image.LANCZOS).save(out_dir / "ai-gateway.png")
    # 多尺寸 ICO：Pillow 逐尺寸 LANCZOS 缩放
    icon.save(out_dir / "ai-gateway.ico",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                     (64, 64), (128, 128), (256, 256)])
    print(f"已生成: {out_dir / 'ai-gateway.ico'}")
    print(f"已生成: {out_dir / 'ai-gateway.png'}")


if __name__ == "__main__":
    main()
