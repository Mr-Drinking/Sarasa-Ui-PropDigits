"""验收样张生成：FreeType 6 地区并排对比样张，以及本机 Windows 96 DPI GDI 与 DirectWrite 私有加载样张。"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
from pathlib import Path
import tempfile
from typing import Sequence
import uuid

import freetype
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw
import uharfbuzz as hb

from .spec import Spec, load_spec

REGION_SAMPLE_LINES: dict[str, str] = {
    "CL": "12:34 x<-1 2026/11/19 巅拼绿㪉优忧扰抚 靇龍 詚詎詛 돌들를름뮋 あいう",
    "SC": "12:34 x<-1 2026/11/19 系统设置：第12项 永中酬靇龍 돌들를름뮋 あいう",
    "TC": "12:34 x<-1 2026/11/19 系統設定：第12項 繁體中文靇龍 돌들를름뮋 ㄅㄆㄇ",
    "HC": "12:34 x<-1 2026/11/19 系統設定：香港字形 溫故知新靇龍 돌들를름뮋 ㄅㄆㄇ",
    "J": "12:34 x<-1 2026/11/19 システム設定：日本語 靇龍 です。ます、 あいうアイウ",
    "K": "12:34 x<-1 2026/11/19 시스템 설정: 한글 靇龍 돌들를름뮋 가나다라",
}


def _render_ft_line(path: Path, text: str, ppem: int, *, hinted: bool = True) -> Image.Image:
    blob = path.read_bytes()
    hb_font = hb.Font(hb.Face(blob))
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(hb_font, buf, {})

    face = freetype.Face(str(path))
    face.set_pixel_sizes(0, ppem)
    flags = freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_NORMAL
    flags |= freetype.FT_LOAD_NO_AUTOHINT if hinted else freetype.FT_LOAD_NO_HINTING

    glyphs: list[tuple[int, int, Image.Image | None]] = []
    pen_x = 0.0
    scale = ppem / max(face.units_per_EM, 1)
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        face.load_glyph(info.codepoint, flags)
        bm = face.glyph.bitmap
        img = Image.frombytes("L", (bm.width, bm.rows), bytes(bm.buffer)) if bm.width and bm.rows else None
        gx = int(round(pen_x + pos.x_offset * scale)) + face.glyph.bitmap_left
        gy = int(round(pos.y_offset * scale)) + face.glyph.bitmap_top
        glyphs.append((gx, gy, img))
        adv = (face.glyph.advance.x / 64.0) if hinted else (pos.x_advance * scale)
        pen_x += max(adv, 0.0)

    h = int(ppem * 1.48) + 6
    baseline = int(ppem * 1.12)
    canvas = Image.new("L", (max(int(pen_x) + 24, 120), h), 255)
    for gx, gy, img in glyphs:
        if img is not None:
            inv = Image.eval(img, lambda p: 255 - p)
            canvas.paste(inv, (gx + 8, baseline - gy), img)
    return canvas


def render_region_comparison_sheet(
    out_png: Path,
    *,
    region: str,
    rows: Sequence[tuple[str, Path, bool]],
    ppems: Sequence[int] = (9, 10, 11, 12, 14, 16, 32, 48),
    text: str | None = None,
) -> Path:
    """生成单地区多字号并排对照图（每项 row 为 (label, font_path, use_hinting)）。"""
    sample_text = text or REGION_SAMPLE_LINES.get(region, REGION_SAMPLE_LINES["SC"])
    strips: list[Image.Image] = []
    max_w = 0
    label_w = 230
    for ppem in ppems:
        for label, path, use_hint in rows:
            line_im = _render_ft_line(path, sample_text, ppem, hinted=use_hint)
            row_im = Image.new("L", (label_w + line_im.width, line_im.height), 255)
            draw = ImageDraw.Draw(row_im)
            draw.text((8, max(2, (line_im.height - 12) // 2)), f"{ppem:2d}px {label}", fill=0)
            row_im.paste(line_im, (label_w, 0))
            strips.append(row_im)
            max_w = max(max_w, row_im.width)
        strips.append(Image.new("L", (max(max_w, 400), 4), 210))

    total_h = sum(im.height + 2 for im in strips) + 12
    sheet = Image.new("L", (max_w + 12, total_h), 255)
    y = 6
    for im in strips:
        sheet.paste(im, (0, y))
        y += im.height + 2
    out_png.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_png)
    return out_png


def _make_private_renamed_copy(src: Path, dst: Path, family: str) -> Path:
    with TTFont(src, recalcBBoxes=False, recalcTimestamp=False) as f:
        for nid in (1, 4, 16):
            f["name"].setName(family, nid, 3, 1, 0x0409)
            f["name"].setName(family, nid, 3, 1, 0x0804)
        f["name"].setName(family.replace(" ", ""), 6, 3, 1, 0x0409)
        f.save(dst, reorderTables=True)
    return dst


def render_windows_gdi_comparison(
    out_png: Path,
    pairs: Sequence[tuple[str, Path, int, bool]],
    *,
    text: str = "Ag0123456789 12:34 x<-1 系统设置 永中靇龍 돌들를름뮋 あいう",
    ppems: Sequence[int] = (12, 14, 16, 32, 48),
) -> Path:
    """在 Windows 上用 GDI FR_PRIVATE 私有加载（不改系统字体）渲染 96 DPI ClearType 对比样张。"""
    if os.name != "nt":
        raise RuntimeError("Windows GDI 渲染仅支持 Windows 平台")

    gdi32 = ctypes.windll.gdi32
    user32 = ctypes.windll.user32
    FR_PRIVATE = 0x10

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
            ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
            ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
            ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
        ]

    with tempfile.TemporaryDirectory(prefix="sarasa-gdi-") as raw_tmp:
        tmp = Path(raw_tmp)
        entries: list[tuple[str, str, int, int, Path]] = []
        for idx, (label, path, weight, italic) in enumerate(pairs):
            fam = f"SarasaGdiPriv{idx}"
            copied = _make_private_renamed_copy(path, tmp / f"{fam}.ttf", fam)
            if gdi32.AddFontResourceExW(str(copied), FR_PRIVATE, 0) <= 0:
                raise RuntimeError(f"AddFontResourceExW 加载失败：{copied}")
            entries.append((label, fam, weight, 1 if italic else 0, copied))

        try:
            W = 1800
            row_h_sum = sum(int(px * 1.4) + 10 for px in ppems for _ in entries) + len(ppems) * 8 + 20
            H = max(row_h_sum, 200)
            hdc_screen = user32.GetDC(0)
            hdc = gdi32.CreateCompatibleDC(hdc_screen)
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = W
            bmi.biHeight = -H
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bits = ctypes.c_void_p()
            hbmp = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            old_bmp = gdi32.SelectObject(hdc, hbmp)

            brush = gdi32.CreateSolidBrush(0x00FFFFFF)
            rc = wt.RECT(0, 0, W, H)
            user32.FillRect(hdc, ctypes.byref(rc), brush)
            gdi32.DeleteObject(brush)
            gdi32.SetBkMode(hdc, 1)
            gdi32.SetTextColor(hdc, 0x00000000)

            y = 10
            for px in ppems:
                for label, fam, weight, italic, _p in entries:
                    hf_lbl = gdi32.CreateFontW(-13, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, "Consolas")
                    gdi32.SelectObject(hdc, hf_lbl)
                    tag = f"{px:2d}px {label}"
                    gdi32.TextOutW(hdc, 10, y + max(0, (px - 13) // 2), tag, len(tag))
                    gdi32.DeleteObject(hf_lbl)

                    hf = gdi32.CreateFontW(-px, 0, 0, 0, weight, italic, 0, 0, 1, 0, 0, 5, 0, fam)
                    gdi32.SelectObject(hdc, hf)
                    gdi32.TextOutW(hdc, 230, y, text, len(text))
                    gdi32.DeleteObject(hf)
                    y += int(px * 1.4) + 10
                y += 8

            buf = (ctypes.c_ubyte * (W * H * 4))()
            ctypes.memmove(buf, bits, W * H * 4)
            img = Image.frombuffer("RGBA", (W, H), bytes(buf), "raw", "BGRA", 0, 1).convert("RGB")
            out_png.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_png)

            gdi32.SelectObject(hdc, old_bmp)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc)
            user32.ReleaseDC(0, hdc_screen)
        finally:
            for _l, _f, _w, _i, copied in entries:
                gdi32.RemoveFontResourceExW(str(copied), FR_PRIVATE, 0)
    return out_png


def render_windows_dwrite_comparison(
    out_png: Path,
    pairs: Sequence[tuple[str, Path, int, bool]],
    *,
    text: str = "Ag0123456789 12:34 x<-1 系统设置 永中靇龍 돌들를름뮋 あいう",
    ppems: Sequence[int] = (12, 14, 16, 32, 48),
) -> Path:
    """直接调用本机 Windows dwrite.dll（IDWriteFactory::CreateFontFileReference，完全私有、不改系统字体）
    渲染 96 DPI DirectWrite ClearType Natural Symmetric 对比样张。"""
    if os.name != "nt":
        raise RuntimeError("Windows DirectWrite 渲染仅支持 Windows 平台")

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class DWRITE_GLYPH_OFFSET(ctypes.Structure):
        _fields_ = [("advanceOffset", ctypes.c_float), ("ascenderOffset", ctypes.c_float)]

    class DWRITE_GLYPH_RUN(ctypes.Structure):
        _fields_ = [
            ("fontFace", ctypes.c_void_p),
            ("fontEmSize", ctypes.c_float),
            ("glyphCount", wt.UINT),
            ("glyphIndices", ctypes.POINTER(ctypes.c_uint16)),
            ("glyphAdvances", ctypes.POINTER(ctypes.c_float)),
            ("glyphOffsets", ctypes.POINTER(DWRITE_GLYPH_OFFSET)),
            ("isSideways", wt.BOOL),
            ("bidiLevel", wt.UINT),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
            ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
            ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
            ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
        ]

    def vcall(obj: ctypes.c_void_p, idx: int, restype, argtypes, *args):
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[idx])
        return fn(obj, *args)

    def release(obj: ctypes.c_void_p | None) -> None:
        if obj and obj.value:
            vcall(obj, 2, wt.ULONG, [])

    dwrite = ctypes.windll.dwrite
    gdi32 = ctypes.windll.gdi32
    user32 = ctypes.windll.user32
    user32.FillRect.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH]
    gdi32.GetCurrentObject.argtypes = [wt.HDC, wt.UINT]
    gdi32.GetCurrentObject.restype = wt.HGDIOBJ
    gdi32.GetDIBits.argtypes = [
        wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT
    ]
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    gdi32.SelectObject.restype = wt.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    gdi32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
    gdi32.SetTextColor.argtypes = [wt.HDC, wt.DWORD]
    gdi32.TextOutW.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, wt.LPCWSTR, ctypes.c_int]

    iid_factory = GUID.from_buffer_copy(uuid.UUID("b859ee5a-d838-4b5b-a2e8-1adc7d93db48").bytes_le)
    factory = ctypes.c_void_p()
    hr = dwrite.DWriteCreateFactory(0, ctypes.byref(iid_factory), ctypes.byref(factory))
    if hr != 0:
        raise RuntimeError(f"DWriteCreateFactory 失败：0x{hr & 0xFFFFFFFF:08X}")

    W = 1800
    H = max(sum(int(px * 1.45) + 10 for px in ppems for _ in pairs) + len(ppems) * 8 + 24, 200)
    com_refs: list[ctypes.c_void_p] = [factory]
    try:
        rparams = ctypes.c_void_p()
        # gamma=1.8, enhancedContrast=0.5, clearTypeLevel=1.0, RGB(1), CLEARTYPE_NATURAL_SYMMETRIC(5)
        hr = vcall(
            factory, 12, ctypes.c_long,
            [ctypes.c_float, ctypes.c_float, ctypes.c_float, wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_void_p)],
            1.8, 0.5, 1.0, 1, 5, ctypes.byref(rparams),
        )
        if hr != 0:
            raise RuntimeError(f"CreateCustomRenderingParams 失败：0x{hr & 0xFFFFFFFF:08X}")
        com_refs.append(rparams)

        gdi_interop = ctypes.c_void_p()
        hr = vcall(factory, 17, ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(gdi_interop))
        if hr != 0:
            raise RuntimeError(f"GetGdiInterop 失败：0x{hr & 0xFFFFFFFF:08X}")
        com_refs.append(gdi_interop)

        rt = ctypes.c_void_p()
        hr = vcall(
            gdi_interop, 7, ctypes.c_long,
            [wt.HDC, wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_void_p)],
            None, W, H, ctypes.byref(rt),
        )
        if hr != 0:
            raise RuntimeError(f"CreateBitmapRenderTarget 失败：0x{hr & 0xFFFFFFFF:08X}")
        com_refs.append(rt)

        hdc = vcall(rt, 4, wt.HDC, [])
        brush = gdi32.CreateSolidBrush(0x00FFFFFF)
        rc = wt.RECT(0, 0, W, H)
        user32.FillRect(hdc, ctypes.byref(rc), brush)
        gdi32.DeleteObject(brush)
        gdi32.SetBkMode(hdc, 1)
        gdi32.SetTextColor(hdc, 0x00000000)

        # 预加载每个字体文件的 DirectWrite FontFace 与 HarfBuzz 塑形结果
        loaded: list[tuple[str, ctypes.c_void_p, int, list[int], list[tuple[int, int, int]]]] = []
        for label, path, _weight, _italic in pairs:
            ff_file = ctypes.c_void_p()
            hr = vcall(
                factory, 7, ctypes.c_long,
                [wt.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                str(path.resolve()), None, ctypes.byref(ff_file),
            )
            if hr != 0:
                raise RuntimeError(f"CreateFontFileReference 失败：{path} (0x{hr & 0xFFFFFFFF:08X})")
            com_refs.append(ff_file)

            ff_face = ctypes.c_void_p()
            arr = (ctypes.c_void_p * 1)(ff_file)
            hr = vcall(
                factory, 9, ctypes.c_long,
                [wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_void_p)],
                1, 1, arr, 0, 0, ctypes.byref(ff_face),
            )
            if hr != 0:
                raise RuntimeError(f"CreateFontFace 失败：{path} (0x{hr & 0xFFFFFFFF:08X})")
            com_refs.append(ff_face)

            blob = path.read_bytes()
            hb_face = hb.Face(blob)
            hb_font = hb.Font(hb_face)
            buf = hb.Buffer()
            buf.add_str(text)
            buf.guess_segment_properties()
            hb.shape(hb_font, buf, {})
            gids = [info.codepoint for info in buf.glyph_infos]
            pos = [(p.x_advance, p.x_offset, p.y_offset) for p in buf.glyph_positions]
            loaded.append((label, ff_face, max(hb_face.upem, 1), gids, pos))

        y = 12
        for px in ppems:
            for label, ff_face, upem, gids, pos in loaded:
                hf_lbl = gdi32.CreateFontW(-13, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, "Consolas")
                old_f = gdi32.SelectObject(hdc, hf_lbl)
                tag = f"{px:2d}px {label}"
                gdi32.TextOutW(hdc, 10, y + max(0, (px - 13) // 2), tag, len(tag))
                gdi32.SelectObject(hdc, old_f)
                gdi32.DeleteObject(hf_lbl)

                n = len(gids)
                c_gids = (ctypes.c_uint16 * n)(*gids)
                c_advs = (ctypes.c_float * n)(*[xa * px / upem for xa, _xo, _yo in pos])
                c_offs = (DWRITE_GLYPH_OFFSET * n)(*[
                    DWRITE_GLYPH_OFFSET(xo * px / upem, yo * px / upem) for _xa, xo, yo in pos
                ])
                run = DWRITE_GLYPH_RUN(ff_face, float(px), n, c_gids, c_advs, c_offs, 0, 0)
                baseline_y = float(y) + float(px) * 1.02
                hr = vcall(
                    rt, 3, ctypes.c_long,
                    [
                        ctypes.c_float, ctypes.c_float, wt.UINT,
                        ctypes.POINTER(DWRITE_GLYPH_RUN), ctypes.c_void_p,
                        wt.DWORD, ctypes.POINTER(wt.RECT),
                    ],
                    230.0, baseline_y, 0, ctypes.byref(run), rparams, 0x00000000, None,
                )
                if hr != 0:
                    raise RuntimeError(f"DrawGlyphRun 失败：{label}@{px}px (0x{hr & 0xFFFFFFFF:08X})")
                y += int(px * 1.45) + 10
            y += 8

        hbmp = gdi32.GetCurrentObject(hdc, 7)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), W, -H, 1, 32, 0, 0, 0, 0, 0, 0)
        buf_px = (ctypes.c_ubyte * (W * H * 4))()
        if gdi32.GetDIBits(hdc, hbmp, 0, H, buf_px, ctypes.byref(bmi), 0) <= 0:
            raise RuntimeError("DirectWrite 位图读取（GetDIBits）失败")
        img = Image.frombuffer("RGBA", (W, H), bytes(buf_px), "raw", "BGRA", 0, 1).convert("RGB")
        out_png.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_png)
    finally:
        for obj in reversed(com_refs):
            release(obj)
    return out_png
