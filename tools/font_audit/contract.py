"""Release specification and paths, deliberately independent of the builder."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = Path(os.environ.get("SARASA_WORK_ROOT", ROOT / ".build-cache/work"))
FONT_ROOT = Path(os.environ.get("SARASA_AUDIT_FONT_ROOT", ROOT / "fonts"))
SOURCE_ROOT = Path(os.environ.get("VF_SOURCE_DIR", WORK / "vf-sources"))
VERSION = "1.0.43.3"
# A project revision keeps the pinned upstream release.
SARASA_VERSION = "1.0.43"
# head.fontRevision and the nameID 5 prefix: 1.0.43 is written 1.043, 1.0.43.1 1.0431.
OPENTYPE_VERSION = "1." + "".join(VERSION.split(".")[1:])
REFERENCE_ROOT = Path(os.environ.get("REFERENCE_SARASA_ROOT", WORK / f"official-sarasa-ui-{SARASA_VERSION}"))
SARASA_SOURCE = Path(os.environ.get("SARASA_SOURCE_DIR", WORK / f"Sarasa-Gothic-{SARASA_VERSION}"))
REGIONS = ("CL", "SC", "TC", "HC", "J", "K")
WEIGHTS = {"ExtraLight": 200, "Light": 300, "Regular": 400,
           "SemiBold": 600, "Bold": 700, "Heavy": 900}
AXIS_POINTS = (200, 250, 300, 325, 350, 375, 400, 500, 600, 650, 700, 800, 900)
CJK_WEIGHTS = {200: 250, 300: 300, 350: 350, 400: 400, 600: 500, 700: 700, 900: 900}
# The declared differences are read from the published list itself, so the
# list and the codepoints the audit treats specially cannot drift apart.
INTENTIONAL_DIFFERENCES = json.loads(Path(__file__).with_name("intentional-differences.json")
                                     .read_text("utf-8"))["differences"]


def declared_codepoints(identifier: str) -> frozenset[int]:
    return frozenset(next(item for item in INTENTIONAL_DIFFERENCES if item["id"] == identifier)["codepoints"])


# The 37 proportional defaults (digits, spaces, punctuation and operators).
INTENTIONAL_CPS = declared_codepoints("proportional-defaults")
# Dashes and ellipses that follow Source Han's (CL: Shanggu's) language routing.
REGIONAL_PUNCTUATION = declared_codepoints("regional-punctuation")
# CL ideographs whose GDEF marking the project corrects to spacing bases.
CLASSICAL_SPACING_BASES = declared_codepoints("classical-spacing-bases")
# Dashes whose vertical runs become joined 1em segments: segments per character.
# U+FE31 is the em dash's vertical form; HarfBuzz hands it over for U+2014
# when only vrt2 is on, and its runs join the same way.
VERTICAL_DASH_SEGMENTS = {0x2014: 1, 0x2E3A: 2, 0x2E3B: 3, 0xFE31: 1}
if set(VERTICAL_DASH_SEGMENTS) != declared_codepoints("vertical-dash-segments"):
    raise ValueError("竖排分段的码位与有意差异清单不一致")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def display_path(path: Path) -> str:
    resolved = path.resolve()
    for prefix, root in (("", ROOT), ("$WORK/", WORK), ("$SOURCES/", SOURCE_ROOT),
                         ("$REFERENCES/", REFERENCE_ROOT), ("$FONTS/", FONT_ROOT), ("$SARASA/", SARASA_SOURCE)):
        if resolved.is_relative_to(root.resolve()):
            return prefix + resolved.relative_to(root.resolve()).as_posix()
    # Never expose workstation paths in a report. A source outside all declared
    # roots has to be configured explicitly instead of silently losing identity.
    raise ValueError(f"审计文件不在已声明的输入目录内：{path.name}")


def style_name(weight: str, italic: bool) -> str:
    return "Italic" if weight == "Regular" and italic else weight + ("Italic" if italic else "")


def static_path(region: str, weight: str, italic: bool, hinted: bool) -> Path:
    variant = "TTF" if hinted else "TTF-Unhinted"
    return (FONT_ROOT / "static" / f"SarasaUiPropDigits{region}-{variant}-{VERSION}"
            / f"SarasaUiPropDigits{region}-{style_name(weight, italic)}.ttf")


def vf_path(region: str, italic: bool) -> Path:
    return FONT_ROOT / "variable" / f"Sarasa-Ui-VF-PropDigits-{region}{'-Italic' if italic else ''}[wght].ttf"


def reference_path(region: str, weight: str, italic: bool, hinted: bool = False) -> Path:
    if weight == "Heavy":
        raise ValueError("上游没有 Heavy Sarasa 静态字体；不得借用 Bold 作为 Heavy 判定基准")
    return (REFERENCE_ROOT / region / ("hinted" if hinted else "unhinted")
            / f"SarasaUi{region}-{style_name(weight, italic)}.ttf")


def inter_path(italic: bool) -> Path:
    return Path(os.environ.get("INTER_ITALIC_VF" if italic else "INTER_VF",
                               SOURCE_ROOT / ("InterVariable-Italic.woff2" if italic else "InterVariable.ttf")))


def cjk_path(region: str) -> Path:
    suffix = {"CL": "K", "J": ""}.get(region, region)
    return Path(os.environ.get(f"SOURCE_HAN_{region}_VF", SOURCE_ROOT / f"SourceHanSans{suffix}-VF.ttf"))


def release_font_paths() -> list[Path]:
    return [path for region in REGIONS for path in (
        *(static_path(region, weight, italic, hinted)
          for hinted in (False, True) for weight in WEIGHTS for italic in (False, True)),
        *(vf_path(region, italic) for italic in (False, True)))]


def release_license_document_paths() -> list[Path]:
    return [ROOT / "LICENSE", ROOT / "NOTICE.md",
            *(static_path(region, "Regular", False, hinted).parent / "LICENSE-Sarasa-Gothic.txt"
              for region in REGIONS for hinted in (False, True))]


def manifest(paths) -> dict:
    return {display_path(path): {"size": path.stat().st_size, "sha256": sha256(path)}
            if path.is_file() else {"missing": True} for path in sorted(set(paths))}


def audit_input_manifest() -> dict:
    return manifest(release_font_paths())


def measurement_contract_manifest() -> dict:
    """Code and locked tools that determine measured values (checkpoint identity)."""
    paths = [*sorted((ROOT / "tools/font_audit").glob("*.py")),
             *sorted((ROOT / "tools/font_audit").glob("*.json")),
             ROOT / "tools/python_env_bootstrap.py", ROOT / "requirements-audit.lock"]
    return {display_path(path): sha256(path) for path in paths}


def audit_contract_manifest() -> dict:
    paths = [*sorted((ROOT / "tools/font_audit").glob("*.py")),
             *sorted((ROOT / "tools/font_audit").glob("*.json")),
             ROOT / "tools/audit_sarasa_ui_propdigits.py", ROOT / "tools/python_env_bootstrap.py",
             ROOT / "tools/package_release.py", ROOT / "tools/check_external_release.py",
             ROOT / "tools/render_visual_checks.py", *sorted(ROOT.glob("requirements-*.lock"))]
    return {display_path(path): sha256(path) for path in paths}
