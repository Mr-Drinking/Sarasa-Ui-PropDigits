from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from python_env_bootstrap import VISUAL_DEPENDENCIES, ensure_project_python

if __name__ == "__main__":
    ensure_project_python(VISUAL_DEPENDENCIES, project_root=Path(__file__).resolve().parents[1], label="visual")

import audit_sarasa_ui_propdigits as audit
import build_sarasa_ui_propdigits_sc as build
import freetype
import uharfbuzz as hb
from PIL import Image, ImageDraw, ImageFont


SAMPLE = "LY GT Go ,; A@B A-B 0123456789 1:2 ķ:2 ș:2 G\u0300\u0301 中文……——（「更纱」） ㄥ \U0010FFFD"
SMALL_SAMPLE = "Aa 09:41 ,; đɨ Ⅎ G\u0360H ㄥ \U0010FFFD"
SMALL_PPEMS = (9, 12, 16)
OUT = build.ROOT / "assets" / "checks"
LABEL_FONT: Path | None = None


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

    def draw(self, image: Image.Image, text: str, origin: tuple[float, float], *, script: str | None = None, language: str | None = None, direction: str = "ltr", features: dict | None = None) -> tuple[float, float]:
        buffer = hb.Buffer(); buffer.add_str(text); buffer.direction = direction
        if script is not None:
            buffer.script = script
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
    path = label_font_path()
    return ImageFont.truetype(str(path), size)


def label_font_path() -> Path:
    # The release's SC font includes the Chinese labels on every platform.
    # A user override also supports a local TTF/OTF/TTC without OS path guesses.
    configured = LABEL_FONT or os.environ.get("SARASA_VISUAL_LABEL_FONT")
    path = Path(configured) if configured else audit.static_path("SC", "Regular", False, False)
    if not path.is_file():
        raise RuntimeError("样张标签字体不存在；请用 --label-font 或 SARASA_VISUAL_LABEL_FONT 指定字体")
    return path


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
    main_height = 150 + 70 * len(rows)
    image = Image.new("RGB", (1880, main_height + (1022 if not variable else 0)), "white")
    draw = ImageDraw.Draw(image)
    title_font = labels(30); label_font = labels(17)
    title = f"Sarasa Ui PropDigits {region} · {build.VERSION} · {'可变 13 点' if variable else '静态 24 字体'}"
    draw.text((30, 20), title, font=title_font, fill=(15, 40, 75))
    draw.text((30, 66), "本表按 Latn、无语言标记排版；CJK 路由见细节图。HarfBuzz + FreeType 实际字体栅格。", font=label_font, fill=(65, 80, 100))
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
        ending, _ = renderer.draw(image, SAMPLE, (242, baseline), script="Latn")
        if ending > 1840:
            raise RuntimeError(f"样张横向超出画布：{path.name}")
        cases.append({"font": audit.display_path(path), "weight": weight, "italic": italic, "hinted": hinted, "pixels": 38})
    small_cases = []
    if not variable:
        draw.text((30, main_height + 10), "小字号原生 hint：像素按 2 倍最近邻放大", font=label_font, fill=(15, 40, 75))
        for column, pixels in enumerate(SMALL_PPEMS):
            draw.text((242 + 530 * column, main_height + 40), f"{pixels} px", font=label_font, fill=(65, 80, 100))
        small_rows = [row for row in rows if row[3]]
        for index, (path, weight, italic, hinted) in enumerate(small_rows):
            top = main_height + 80 + index * 76
            draw.text((30, top + 23), f"{weight} {'斜体' if italic else '正体'}", font=label_font, fill=(65, 80, 100))
            for column, pixels in enumerate(SMALL_PPEMS):
                tile = Image.new("RGB", (256, 32), "white")
                renderer = FontRenderer(path, pixels)
                ending, _ = renderer.draw(tile, SMALL_SAMPLE, (2, 22))
                if ending > 252:
                    raise RuntimeError(f"小字号样张横向超出画布：{path.name}")
                image.paste(tile.resize((512, 64), Image.Resampling.NEAREST), (242 + 530 * column, top))
                small_cases.append({"font": audit.display_path(path), "weight": weight, "italic": italic, "pixels": pixels, "magnification": 2})
    file = OUT / f"{region}-{'variable' if variable else 'static'}.png"
    image.save(file)
    return {"file": audit.display_path(file), "sha256": build.file_sha256(file), "region": region, "variable": variable, "cases": cases, "small_size_cases": small_cases}


def detail_sheet(region: str) -> dict:
    image = Image.new("RGB", (1880, 3240), "white")
    draw = ImageDraw.Draw(image); title_font = labels(30); label_font = labels(20)
    draw.text((32, 22), f"{region} · 定位与默认竖排放大检查", font=title_font, fill=(15, 40, 75))
    regular = FontRenderer(audit.vf_path(region, False), 104)
    italic = FontRenderer(audit.vf_path(region, True), 104)
    tabular = FontRenderer(audit.vf_path(region, False), 44)
    cases = []
    for index, weight in enumerate((200, 400, 600, 900)):
        regular.weight(weight)
        italic.weight(weight); tabular.weight(weight)
        baseline = 208 + 300 * index
        draw.text((34, baseline - 112), f"wght {weight}", font=label_font, fill=(65, 80, 100))
        regular.draw(image, "LY GT Go ,; G\u0300\u0301", (175, baseline), language="en")
        draw.text((1190, baseline - 126), "斜体标点", font=label_font, fill=(65, 80, 100))
        italic.draw(image, ",; ,;", (1190, baseline), language="en")
        delta = audit.inter_positioning_signature(regular.font, "LY", "kern", source=False)[0][0]
        draw.text((175, baseline + 25), f"LY 实际字偶距调整：{delta} units", font=label_font, fill=(65, 80, 100))
        tabular.draw(image, "0123456789 1:2 1: :2 ķ:2 ļ:2 ņ:2 ŗ:2 ș:2 ț:2", (175, baseline + 109), features={"tnum": True, "zero": True})
        draw.text((175, baseline + 134), "tnum + zero：数字严格等宽；边界冒号上浮；小写字母旁的冒号保持原位", font=label_font, fill=(65, 80, 100))
        cases.append({"font": audit.display_path(audit.vf_path(region, False)), "weight": weight, "pixels": 104, "LY_adjustment": delta, "italic_font": audit.display_path(audit.vf_path(region, True)), "combination_features": {"tnum": True, "zero": True}})
    vertical = FontRenderer(audit.vf_path(region, False), 45); vertical.weight(400)
    draw.text((1570, 85), "默认竖排", font=label_font, fill=(65, 80, 100))
    vertical.draw(image, "中文……——排版", (1670, 145), direction="ttb")
    draw.line((30, 1432, 1850, 1432), fill=(210, 220, 230), width=2)
    draw.text((32, 1450), "400：语言与文字系统", font=label_font, fill=(15, 40, 75))
    punctuation = "… — —— ⸺ ——— ⸻"
    language_rows = (("Latn", None), ("Latn", "de"), ("Cyrl", "ru"), ("Grek", "el"),
                     ("Latn", "zh-Hans"), ("Latn", "ko"), ("Hani", None))
    renderer = FontRenderer(audit.vf_path(region, False), 28); renderer.weight(400)
    language_cases = []
    for index, (script, language) in enumerate(language_rows):
        baseline = 1510 + index * 37
        draw.text((34, baseline - 24), f"{script} / {language or '无语言'}", font=label_font, fill=(65, 80, 100))
        renderer.draw(image, punctuation, (258, baseline), script=script, language=language)
        language_cases.append({"font": audit.display_path(audit.vf_path(region, False)), "weight": 400,
                               "script": script, "language": language, "text": punctuation, "pixels": 28})
    draw.text((990, 1450), "37 个比例码位：默认 / pnum / tnum", font=label_font, fill=(15, 40, 75))
    width_text = "".join(chr(cp) for cp in sorted(audit.INTENTIONAL_CPS))
    widths = FontRenderer(audit.vf_path(region, False), 25); widths.weight(400)
    width_cases = []
    for index, features in enumerate(({}, {"pnum": True}, {"tnum": True})):
        baseline = 1518 + index * 82
        draw.text((990, baseline - 43), next(iter(features), "默认"), font=label_font, fill=(65, 80, 100))
        end, _ = widths.draw(image, width_text, (990, baseline), script="Latn", features=features)
        if end > 1850: raise RuntimeError("37 个比例码位的样张超出画布")
        width_cases.append({"font": audit.display_path(audit.vf_path(region, False)), "weight": 400,
                            "features": features, "text": width_text, "pixels": 25})
    draw.text((32, 1790), "400：竖排 1em 片段与静态 / VF 对照（两组分别检查 2em 与 3em）", font=label_font, fill=(15, 40, 75))
    draw.text((32, 2420), "400：长串连续连接（4 段与混合编码、16 段）", font=label_font, fill=(15, 40, 75))
    vertical_cases = []
    for panel, baseline in enumerate((1890, 2520)):
        column = 0
        for variable in (True, False):
            for is_italic in (False, True):
                path = audit.vf_path(region, is_italic) if variable else audit.static_path(region, "Regular", is_italic, False)
                renderer = FontRenderer(path, 34)
                if variable: renderer.weight(400)
                for offset, text in enumerate(build.VISUAL_VERTICAL_RUN_TEXTS[panel * 2:panel * 2 + 2]):
                    x = 120 + 218 * column; column += 1
                    label = ("2em", "3em", "4 段＋混合", "16 段")[panel * 2 + offset]
                    draw.text((x - 80, baseline - 50), f"{'VF' if variable else '静态'} {'斜体' if is_italic else '正体'} · {label}", font=label_font, fill=(65, 80, 100))
                    for line in range(13 if panel == 0 else 19):
                        draw.line((x - 55, baseline + line * 34, x + 55, baseline + line * 34), fill=(231, 237, 243))
                    _, end = renderer.draw(image, text, (x, baseline), script="Hani", direction="ttb")
                    if end > (2310 if panel == 0 else 3210): raise RuntimeError("竖排分段样张超出画布")
                    vertical_cases.append({"font": audit.display_path(path), "weight": 400, "italic": is_italic,
                                           "variable": variable, "script": "Hani", "direction": "ttb", "text": text, "pixels": 34})
    file = OUT / f"{region}-detail.png"; image.save(file)
    return {"file": audit.display_path(file), "sha256": build.file_sha256(file), "region": region, "cases": cases,
            "language_cases": language_cases, "width_cases": width_cases, "vertical_cases": vertical_cases}


def main() -> None:
    global LABEL_FONT
    parser = argparse.ArgumentParser(description="用实际成品生成字体视觉样张；生成结果须人工查看，不自动声明通过。")
    parser.add_argument("--regions", default=",".join(build.REGION_ORDER))
    parser.add_argument("--label-font", type=Path, help="样张标签使用的 TTF/OTF/TTC；默认使用本项目 SC Regular unhinted 字体。")
    args = parser.parse_args()
    LABEL_FONT = args.label_font
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
    candidate["reviewed_fonts"] = []
    candidate["label_font"] = {"file": build.portable_report_path(label_font_path()), "sha256": build.file_sha256(label_font_path())}
    path = build.ROOT / "reports" / "visual-candidates.json"
    build.write_json_atomic(path, candidate)
    print(f"[visual] {audit.display_path(path)}，等待实际查看。", flush=True)


if __name__ == "__main__":
    main()
