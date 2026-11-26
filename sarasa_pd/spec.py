"""从 spec.toml 加载只读规格数据并提供纯函数派生查询。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import tomllib
from typing import Any


SPEC_PATH = Path(__file__).with_name("spec.toml")


@dataclass(frozen=True)
class WeightSpec:
    name: str
    value: int
    range_min: int
    range_max: int
    flags: int
    underline_thickness: int
    underline_position: int
    strikeout_size: int
    x_avg_char_width_upright: int
    x_avg_char_width_italic: int
    sarasa_ref_weight: str
    hcfg: str


@dataclass(frozen=True)
class RegionSpec:
    code: str
    order: int
    source_han_vf: str
    classical_vf: str | None
    sarasa_prefix: str
    local_family: str
    local_lang_id: int
    default_ot_lang: str
    codepage_range1: int
    classical: bool


@dataclass(frozen=True)
class ArchivePin:
    key: str
    filename: str
    url: str
    sha256: str


@dataclass(frozen=True)
class DifferenceSpec:
    id: str
    kind: str
    title: str
    basis: str


@dataclass(frozen=True)
class Spec:
    version: str
    previous_version: str
    sarasa_version: str
    vendor_id: str
    head_modified_timestamp: int
    italic_angle: float
    copyright: str
    shanggu_copyright: str
    license_description: str
    license_url: str
    source_only_legal_name_ids: frozenset[int]
    smart_dropout_prep: bytes
    gasp_range_all: int
    lowest_rec_ppem: int

    source_han_limit: tuple[int, int, int]
    public_limit: tuple[int, int, int]
    inter_opsz: int
    public_to_internal_wght: dict[int, int]
    vertical_metrics: dict[str, int]

    weights: tuple[WeightSpec, ...]
    regions: dict[str, RegionSpec]
    region_order: tuple[str, ...]

    propdigits_codepoints: frozenset[int]
    tnum_extra_codepoints: frozenset[int]
    tabular_space_codepoints: frozenset[int]
    dash_cmap_codepoints: frozenset[int]
    dash_locl_codepoints: frozenset[int]
    ellipsis_codepoint: int
    cjk_ellipsis_codepoint: int
    source_han_forced_codepoints: frozenset[int]
    classical_spacing_bases: frozenset[int]
    classical_fullwidth_check: frozenset[int]
    ui_autohint_options: tuple[str, ...]
    ui_autohint_unscaled: frozenset[int]
    sanitizer_pwid: dict[int, str]

    features: dict[str, frozenset[str]]
    archives: dict[str, ArchivePin]
    sarasa_ui_unhinted_sha256: dict[str, str]
    sarasa_ui_hinted_sha256: dict[str, str]
    node_version: str
    node_sha256: dict[str, str]
    ots_version: str
    ots_sha256: dict[str, str]
    source_tags: dict[str, str]
    differences: tuple[DifferenceSpec, ...]

    def opentype_version(self) -> str:
        return opentype_version(self.version)

    def font_revision(self) -> float:
        return float(self.opentype_version())

    def region(self, code: str) -> RegionSpec:
        key = code.upper()
        if key not in self.regions:
            raise ValueError(f"未知地区 {code!r}，可选：{', '.join(self.region_order)}")
        return self.regions[key]

    def weight(self, name: str) -> WeightSpec:
        for item in self.weights:
            if item.name.lower() == name.lower():
                return item
        valid = ", ".join(item.name for item in self.weights)
        raise ValueError(f"未知字重 {name!r}，可选：{valid}")

    def project_copyright(self, region: str) -> str:
        spec = self.region(region)
        if spec.classical:
            return f"{self.copyright} {self.shanggu_copyright}"
        return self.copyright

    def vf_family(self, region: str) -> str:
        return f"Sarasa Ui VF PD {self.region(region).code}"

    def vf_ps_family(self, region: str) -> str:
        return f"Sarasa-Ui-VF-PD-{self.region(region).code}"

    def vf_family_local(self, region: str) -> str:
        r = self.region(region)
        return f"{r.local_family} Ui VF PD {r.code}"

    def static_family(self, region: str) -> str:
        return f"Sarasa Ui PD {self.region(region).code}"

    def static_ps_family(self, region: str) -> str:
        return f"Sarasa-Ui-PD-{self.region(region).code}"

    def static_family_local(self, region: str) -> str:
        r = self.region(region)
        return f"{r.local_family} Ui PD {r.code}"

    def variable_filename(self, region: str, italic: bool) -> str:
        code = self.region(region).code
        suffix = f"{code}-Italic[wght].ttf" if italic else f"{code}[wght].ttf"
        return f"Sarasa-Ui-VF-PropDigits-{suffix}"

    def static_filename(self, region: str, weight_name: str, italic: bool) -> str:
        code = self.region(region).code
        w = self.weight(weight_name).name
        style = "Italic" if (w == "Regular" and italic) else f"{w}{'Italic' if italic else ''}"
        return f"SarasaUiPropDigits{code}-{style}.ttf"

    def static_dirname(self, region: str, hinted: bool, version: str | None = None) -> str:
        code = self.region(region).code
        variant = "TTF" if hinted else "TTF-Unhinted"
        return f"SarasaUiPropDigits{code}-{variant}-{version or self.version}"

    def static_dir(self, root: Path, region: str, hinted: bool, version: str | None = None) -> Path:
        return root / "static" / self.static_dirname(region, hinted=hinted, version=version)

    def sarasa_ref_filename(self, region: str, weight_name: str, italic: bool) -> str:
        r = self.region(region)
        ref_w = self.weight(weight_name).sarasa_ref_weight
        style = "Italic" if (ref_w == "Regular" and italic) else f"{ref_w}{'Italic' if italic else ''}"
        return f"{r.sarasa_prefix}-{style}.ttf"


def opentype_version(version: str) -> str:
    """将 1.0.43.4 转为 OpenType head.fontRevision 字符串 1.0434。"""
    parts = version.split(".")
    if (
        len(parts) not in (3, 4)
        or parts[:2] != ["1", "0"]
        or len(parts[2]) != 2
        or not parts[2].isdigit()
        or (len(parts) == 4 and not (len(parts[3]) == 1 and "1" <= parts[3] <= "9"))
    ):
        raise ValueError(f"版本号 {version!r} 无法无歧义地写成 head.fontRevision")
    return "1." + "".join(parts[1:])


def parse_utc_timestamp(iso_text: str) -> int:
    dt = datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _parse_int_key_map(raw: dict[str, Any]) -> dict[int, Any]:
    return {int(k, 0): v for k, v in raw.items()}


@lru_cache(maxsize=4)
def load_spec(path: Path = SPEC_PATH) -> Spec:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    proj = raw["project"]
    axes = raw["axes"]
    cps = raw["codepoints"]
    feats = raw["features"]
    srcs = raw["sources"]

    weights = tuple(
        WeightSpec(
            name=item["name"],
            value=int(item["value"]),
            range_min=int(item["range_min"]),
            range_max=int(item["range_max"]),
            flags=int(item["flags"]),
            underline_thickness=int(item["underline_thickness"]),
            underline_position=int(item["underline_position"]),
            strikeout_size=int(item["strikeout_size"]),
            x_avg_char_width_upright=int(item["x_avg_char_width_upright"]),
            x_avg_char_width_italic=int(item["x_avg_char_width_italic"]),
            sarasa_ref_weight=item["sarasa_ref_weight"],
            hcfg=item["hcfg"],
        )
        for item in raw["weights"]
    )

    regions: dict[str, RegionSpec] = {}
    for code, item in raw["regions"].items():
        regions[code] = RegionSpec(
            code=code,
            order=int(item["order"]),
            source_han_vf=item["source_han_vf"],
            classical_vf=item.get("classical_vf"),
            sarasa_prefix=item["sarasa_prefix"],
            local_family=item["local_family"],
            local_lang_id=int(item["local_lang_id"]),
            default_ot_lang=str(item["default_ot_lang"]),
            codepage_range1=int(item["codepage_range1"]),
            classical=bool(item["classical"]),
        )
    region_order = tuple(sorted(regions, key=lambda c: regions[c].order))

    archives = {
        key: ArchivePin(
            key=key,
            filename=item["filename"],
            url=item["url"],
            sha256=item["sha256"].lower(),
        )
        for key, item in srcs["archives"].items()
    }

    differences = tuple(
        DifferenceSpec(
            id=item["id"],
            kind=item["kind"],
            title=item["title"],
            basis=item["basis"],
        )
        for item in raw["differences"]
    )

    return Spec(
        version=proj["version"],
        previous_version=proj["previous_version"],
        sarasa_version=proj["sarasa_version"],
        vendor_id=proj["vendor_id"],
        head_modified_timestamp=parse_utc_timestamp(proj["head_modified_utc"]),
        italic_angle=float(proj["italic_angle"]),
        copyright=proj["copyright"],
        shanggu_copyright=proj["shanggu_copyright"],
        license_description=proj["license_description"],
        license_url=proj["license_url"],
        source_only_legal_name_ids=frozenset(int(x) for x in proj["source_only_legal_name_ids"]),
        smart_dropout_prep=bytes.fromhex(proj["smart_dropout_prep_hex"]),
        gasp_range_all=int(proj["gasp_range_all"]),
        lowest_rec_ppem=int(proj["lowest_rec_ppem"]),
        source_han_limit=tuple(int(x) for x in axes["source_han_limit"]),
        public_limit=tuple(int(x) for x in axes["public_limit"]),
        inter_opsz=int(axes["inter_opsz"]),
        public_to_internal_wght={int(k): int(v) for k, v in axes["public_to_internal_wght"].items()},
        vertical_metrics={k: int(v) for k, v in raw["vertical_metrics"].items()},
        weights=weights,
        regions=regions,
        region_order=region_order,
        propdigits_codepoints=frozenset(int(x) for x in cps["propdigits"]),
        tnum_extra_codepoints=frozenset(int(x) for x in cps["tnum_extra"]),
        tabular_space_codepoints=frozenset(int(x) for x in cps["tabular_space"]),
        dash_cmap_codepoints=frozenset(int(x) for x in cps["dash_cmap"]),
        dash_locl_codepoints=frozenset(int(x) for x in cps["dash_locl"]),
        ellipsis_codepoint=int(cps["ellipsis"]),
        cjk_ellipsis_codepoint=int(cps["cjk_ellipsis"]),
        source_han_forced_codepoints=frozenset(int(x) for x in cps["source_han_forced"]),
        classical_spacing_bases=frozenset(int(x) for x in cps["classical_spacing_bases"]),
        classical_fullwidth_check=frozenset(int(x) for x in cps["classical_fullwidth_check"]),
        ui_autohint_options=tuple(str(x) for x in cps["ui_autohint_options"]),
        ui_autohint_unscaled=frozenset(int(x) for x in cps["ui_autohint_unscaled"]),
        sanitizer_pwid={int(k, 0): str(v) for k, v in cps["sanitizer_pwid"].items()},
        features={k: frozenset(str(x) for x in v) for k, v in feats.items()},
        archives=archives,
        sarasa_ui_unhinted_sha256={k: v.lower() for k, v in srcs["sarasa_ui_unhinted_sha256"].items()},
        sarasa_ui_hinted_sha256={k: v.lower() for k, v in srcs["sarasa_ui_hinted_sha256"].items()},
        node_version=srcs["node_version"],
        node_sha256={k: v.lower() for k, v in srcs["node_sha256"].items()},
        ots_version=srcs["ots_version"],
        ots_sha256={k: v.lower() for k, v in srcs["ots_sha256"].items()},
        source_tags={
            "source_han": srcs["source_han_tag"],
            "inter": srcs["inter_tag"],
            "shanggu": srcs["shanggu_tag"],
            "sarasa": srcs["sarasa_tag"],
        },
        differences=differences,
    )


# ---- 码位分类纯函数（沿用 Sarasa 边界语义）----

def is_ideograph(c: int) -> bool:
    return (
        0x2E80 <= c <= 0x2FFF
        or 0x3192 <= c <= 0x319F
        or 0x31C0 <= c <= 0x31EF
        or 0x3400 <= c <= 0x4DBF
        or 0x4E00 <= c <= 0x9FFF
        or 0xF900 <= c <= 0xFA6F
        or 0x20000 <= c <= 0x3FFFF
    )


def is_western(c: int) -> bool:
    return (c < 0x2000 and c != 0x00B7) or (0x2070 <= c <= 0x218F)


def is_korean(c: int) -> bool:
    return (
        0x1100 <= c <= 0x11FF
        or 0xAC00 <= c <= 0xD7AF
        or 0x3130 <= c <= 0x318F
        or 0x3200 <= c <= 0x321E
        or 0xFFA1 <= c <= 0xFFDC
        or 0x3260 <= c <= 0x327F
        or 0xA960 <= c <= 0xA97F
        or 0xD7B0 <= c <= 0xD7FF
    )


def is_kana(c: int) -> bool:
    return (
        0x3040 <= c <= 0x30FF
        or 0x31F0 <= c <= 0x31FF
        or 0xFF65 <= c <= 0xFF9F
        or 0x1B000 <= c <= 0x1B16F
    )


def is_enclosed_alphanumerics(c: int) -> bool:
    return 0x20DD <= c <= 0x20DE or 0x2460 <= c <= 0x24FF or 0x2776 <= c <= 0x2788


def is_pua(c: int) -> bool:
    return 0xE000 <= c <= 0xF8FF


def is_fe_misc(c: int) -> bool:
    return (
        0x3003 <= c <= 0x3007
        or 0x3012 <= c <= 0x3013
        or 0x3020 <= c <= 0x33FF
        or 0x1AFF0 <= c <= 0x1B12F
        or 0x1F000 <= c <= 0x1F2FF
    )


def is_locale_dependent_fwid_punct(c: int) -> bool:
    return c in {
        0xFF01, 0xFF08, 0xFF09, 0xFF0C, 0xFF0E, 0xFF1A,
        0xFF1B, 0xFF3B, 0xFF3D, 0xFF5B, 0xFF5D, 0xFF1F,
    }


def is_ws(c: int) -> bool:
    return (
        (((0x2000 <= c <= 0x200F) or (0x20A0 <= c < 0x3000)) and not (0x2E3A <= c <= 0x2E3B))
        or (0xFF01 <= c <= 0xFF5E and not is_locale_dependent_fwid_punct(c))
    )


def source_han_overrides_inter(c: int, forced: frozenset[int] = frozenset({0x22EF})) -> bool:
    return (
        c in forced
        or is_ideograph(c)
        or is_korean(c)
        or is_enclosed_alphanumerics(c)
        or is_pua(c)
        or (not is_western(c) and not is_ws(c) and not is_fe_misc(c))
    )


def use_inter_codepoint(c: int, forced: frozenset[int] = frozenset({0x22EF})) -> bool:
    return not source_han_overrides_inter(c, forced)


def codepoint_category(c: int) -> str:
    """对照报告使用的粗粒度码位分类。"""
    if 0x30 <= c <= 0x39:
        return "digit"
    if c in {0x2014, 0x2015, 0x2025, 0x2026, 0x22EF, 0x2E3A, 0x2E3B, 0xFE31}:
        return "dash_ellipsis"
    if is_ideograph(c):
        return "ideograph"
    if is_korean(c):
        return "hangul"
    if is_kana(c):
        return "kana"
    if is_western(c):
        return "western"
    return "symbol_punct"
