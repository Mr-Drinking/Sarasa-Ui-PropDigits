"""Prepare fixed upstream fixtures for CI without building or hinting fonts."""
from __future__ import annotations

import io
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as build
from fontTools.ttLib import TTCollection


def main() -> None:
    build.ensure_inter_sources()
    build.ensure_vf_sources(["SC"])
    build.ensure_reference_sarasa(list(build.REGION_ORDER), hinted_styles=(False,))
    archive = build.SOURCE_ARCHIVE_DIR / "Inter-4.1.zip"
    if build.file_sha256(archive) != build.INTER_ARCHIVE_SHA256:
        raise ValueError("Inter 测试来源归档哈希不匹配")
    with zipfile.ZipFile(archive) as package:
        collection = TTCollection(io.BytesIO(package.read("Inter.ttc")))
    try:
        found = set()
        for font in collection.fonts:
            name = font["name"].getDebugName(6)
            if not name or not name.startswith("Inter-"):
                continue
            output = io.BytesIO(); font.recalcTimestamp = False; font.save(output)
            build.verify_or_write_source(build.SARASA_SOURCE_DIR / "sources/Inter" / f"{name}.ttf", output.getvalue())
            found.add(name)
        required = {"Inter-Regular", "Inter-ExtraLight"}
        if not required <= found:
            raise ValueError("固定 Inter TTC 缺少静态测试夹具")
    finally:
        collection.close()
    print("已准备固定 Inter 静态/VF、Source Han SC 和六地区 Sarasa 原生参考。")


if __name__ == "__main__":
    main()
