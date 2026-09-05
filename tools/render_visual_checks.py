from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import audit_sarasa_ui_propdigits as audit
import build_sarasa_ui_propdigits_sc as build
from python_env_bootstrap import ensure_project_python

ensure_project_python({"PIL": ("Pillow", "Pillow==12.3.0", "12.3.0")}, project_root=build.ROOT, label="visual")
import freetype
import uharfbuzz as hb
from PIL import Image, ImageDraw, ImageFont


SAMPLE = "LY GT Go ĢT 0123456789 1:2 G\u0300\u0301 中文……——（「更纱」）"
OUT = build.ROOT / "assets" / "checks"


class FontRenderer:
    def __init__(self, path: Path, pixels: int):
        self.face = freetype.Face(str(path))
        self.face.set_pixel_sizes(0, pixels)
        self.font = hb.Font(hb.Face(path.read_bytes()))
        self.pixels = pixels
        self.factor = pixels / self.font.face.upem

    def weight(self, value: int) -> None:
        self.face.set_var_design_coords([value])
        self.font.set_variations({"wght": value})

    def draw(self, image: Image.Image, text: str, origin: tuple[float, float], *, language: str | None = None, direction: str = "ltr", features: dict | None = None) -> tuple[float, float]:
        buffer = hb.Buffer(); buffer.add_str(text); buffer.direction = direction
        if language is not None:
            buffer.language = language
        buffer.guess_segment_properties()
        hb.shape(self.font, buffer, features)
        x, baseline = origin
        for info, position in zip(buffer.glyph_infos, buffer.glyph_positions):
            self.face.load_glyph(info.codepoint, freetype.FT_LOAD_RENDER | freetype.FT_LOAD_NO_BITMAP)
            slot = self.face.glyph; bitmap = slot.bitmap
            if bitmap.width and bitmap.rows:
                mask = Image.frombytes("L", (bitmap.width, bitmap.rows), bytes(bitmap.buffer), "raw", "L", bitmap.pitch)
                left = round(x + position.x_offset * self.factor + slot.bitmap_left)
                top = round(baseline - position.y_offset * self.factor - slot.bitmap_top)
                image.paste((20, 25, 35), (left, top), mask)
            x += position.x_advance * self.factor
            baseline -= position.y_advance * self.factor
        return x, baseline


def labels(size: int):
    for name in ("C:/Windows/Fonts/msyh.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    raise RuntimeError("缺少样张标签字体")


def sheet(region: str, variable: bool) -> dict:
    rows = []
    if variable:
        for weight in audit.INTER_POSITION_WEIGHTS:
            for italic in (False, True):
                rows.append((audit.vf_path(region, italic), weight, italic, None))
    else:
        for stop in build.SOURCE_HAN_WEIGHT_STOPS:
            for italic in (False, True):
                for hinted in (False, True):
                    rows.append((audit.static_path(region, str(stop["name"]), italic, hinted), int(stop["value"]), italic, hinted))
    image = Image.new("RGB", (1880, 150 + 70 * len(rows)), "white")
    draw = ImageDraw.Draw(image)
    title_font = labels(30); label_font = labels(17)
    title = f"Sarasa Ui PropDigits {region} · {build.VERSION} · {'可变 13 点' if variable else '静态 24 字体'}"
    draw.text((30, 20), title, font=title_font, fill=(15, 40, 75))
    draw.text((30, 66), "未提供语言标记；HarfBuzz 排版 + FreeType 实际字体栅格。列间浅线为比较基线。", font=label_font, fill=(65, 80, 100))
    renderers = {}
    cases = []
    for index, (path, weight, italic, hinted) in enumerate(rows):
        baseline = 141 + 70 * index
        if index % 2 == 0:
            draw.rectangle((18, baseline - 45, 1862, baseline + 18), fill=(247, 249, 252))
        draw.line((232, baseline + 2, 1838, baseline + 2), fill=(224, 231, 238), width=1)
        label = f"{weight} {'斜体' if italic else '正体'}"
        if hinted is not None:
            label += " / hinted" if hinted else " / unhinted"
        elif weight == 350:
            label += " / 隐藏锚点"
        draw.text((30, baseline - 23), label, font=label_font, fill=(65, 80, 100))
        if path not in renderers:
            renderers[path] = FontRenderer(path, 38)
        renderer = renderers[path]
        if variable:
            renderer.weight(weight)
        ending, _ = renderer.draw(image, SAMPLE, (242, baseline))
        if ending > 1840:
            raise RuntimeError(f"样张横向超出画布：{path.name}")
        cases.append({"font": audit.display_path(path), "weight": weight, "italic": italic, "hinted": hinted, "pixels": 38})
    file = OUT / f"{region}-{'variable' if variable else 'static'}.png"
    image.save(file)
    return {"file": audit.display_path(file), "sha256": build.file_sha256(file), "region": region, "variable": variable, "cases": cases}


def detail_sheet(region: str) -> dict:
    image = Image.new("RGB", (1850, 1180), "white")
    draw = ImageDraw.Draw(image); title_font = labels(30); label_font = labels(20)
    draw.text((32, 22), f"{region} · 定位与默认竖排放大检查", font=title_font, fill=(15, 40, 75))
    regular = FontRenderer(audit.vf_path(region, False), 104)
    cases = []
    for index, weight in enumerate((200, 400, 600, 900)):
        regular.weight(weight)
        baseline = 208 + 224 * index
        draw.text((34, baseline - 112), f"wght {weight}", font=label_font, fill=(65, 80, 100))
        regular.draw(image, "LY GT Go ĢT G\u0300\u0301", (175, baseline), language="en")
        delta = audit.inter_positioning_signature(regular.font, "LY", "kern", source=False)[0][0]
        draw.text((175, baseline + 25), f"LY 实际字偶距调整：{delta} units", font=label_font, fill=(65, 80, 100))
        cases.append({"font": audit.display_path(audit.vf_path(region, False)), "weight": weight, "pixels": 104, "LY_adjustment": delta})
    vertical = FontRenderer(audit.vf_path(region, False), 45); vertical.weight(400)
    draw.text((1570, 85), "默认竖排", font=label_font, fill=(65, 80, 100))
    vertical.draw(image, "中文……——排版", (1670, 145), direction="ttb")
    file = OUT / f"{region}-detail.png"; image.save(file)
    return {"file": audit.display_path(file), "sha256": build.file_sha256(file), "region": region, "cases": cases}


def main() -> None:
    parser = argparse.ArgumentParser(description="用实际成品生成字体视觉样张；生成结果须人工查看，不自动声明通过。")
    parser.add_argument("--regions", default=",".join(build.REGION_ORDER))
    args = parser.parse_args()
    regions = build.parse_regions(args.regions)
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = audit.audit_input_manifest()
    if any(item.get("missing") for item in manifest.values()):
        parser.error("字体清单不完整")
    images = []
    for region in regions:
        print(f"[visual] {region}", flush=True)
        images.extend((sheet(region, False), sheet(region, True), detail_sheet(region)))
    if manifest != audit.audit_input_manifest():
        raise RuntimeError("生成样张期间成品发生变化")
    candidate = {"title": "字体视觉检查候选样张", "generated_at": datetime.now(timezone.utc).isoformat(), "complete": False, "passed": False, "review_required": True, "input_manifest": manifest, "generator": "tools/render_visual_checks.py", "generator_sha256": build.file_sha256(Path(__file__)), "engines": {"HarfBuzz": hb.version_string(), "FreeType": list(freetype.version())}, "regions": regions, "images": images}
    path = build.ROOT / "reports" / "visual-candidates.json"
    build.write_json_atomic(path, candidate)
    print(f"[visual] {audit.display_path(path)}，等待实际查看。", flush=True)


if __name__ == "__main__":
    main()
