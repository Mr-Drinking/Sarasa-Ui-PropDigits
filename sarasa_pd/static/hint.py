"""混合静态 Hinting：ttfautohint（西文/数字）+ 独立精简 Chlorophytum（CJK/假名/韩文）+ 确定性持久缓存 + 斜体复用 + cvt 预算扩充。"""
from __future__ import annotations

import array
from concurrent.futures import ProcessPoolExecutor, as_completed
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any

from fontTools.ttLib import TTFont

from ..env import repo_root, selected_ttfautohint
from ..sources import SourceStore
from ..spec import Spec, WeightSpec, is_ideograph, is_kana, is_korean, load_spec
from ..vf.names import apply_deterministic_head
from .instance import update_static_names

DEFAULT_CVT_MIN_LEN = 16384


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_hint_env(work_root: Path, hint_env_dir: Path, spec: Spec) -> tuple[Path, Path]:
    """确保锁定 Node 与独立精简 Chlorophytum 环境就绪，并同步 hcfg 到该环境目录。"""
    store = SourceStore(work_root, spec)
    node_exe = store.ensure_node()
    hint_src = repo_root() / "hint"
    pkg_json = hint_src / "package.json"
    pkg_lock = hint_src / "package-lock.json"
    if not pkg_json.is_file() or not pkg_lock.is_file():
        raise FileNotFoundError(f"缺少 hint/package.json 或 package-lock.json：{hint_src}")

    hint_env_dir.mkdir(parents=True, exist_ok=True)
    lock_digest = _sha256_bytes(pkg_lock.read_bytes())
    stamp_path = hint_env_dir / ".lock-sha256"
    cli_js = hint_env_dir / "node_modules" / "@chlorophytum" / "cli" / "lib" / "index.js"

    if not cli_js.is_file() or not stamp_path.is_file() or stamp_path.read_text(encoding="ascii").strip() != lock_digest:
        shutil.copy2(pkg_json, hint_env_dir / "package.json")
        shutil.copy2(pkg_lock, hint_env_dir / "package-lock.json")
        npm_cmd = node_exe.parent / ("npm.cmd" if os.name == "nt" else "npm")
        env = os.environ.copy()
        env["PATH"] = str(node_exe.parent) + os.pathsep + env.get("PATH", "")
        proc = subprocess.run(
            [str(npm_cmd), "ci", "--ignore-scripts"],
            cwd=str(hint_env_dir),
            capture_output=True,
            text=True,
            env=env,
        )
        if proc.returncode != 0 or not cli_js.is_file():
            raise RuntimeError(f"Chlorophytum 依赖安装失败（rc={proc.returncode}）：{proc.stderr or proc.stdout}")
        stamp_path.write_text(lock_digest + "\n", encoding="ascii")

    hcfg_dst = hint_env_dir / "hcfg"
    hcfg_dst.mkdir(parents=True, exist_ok=True)
    for src in (hint_src / "hcfg").glob("*.json"):
        dst = hcfg_dst / src.name
        payload = src.read_bytes()
        if not dst.is_file() or dst.read_bytes() != payload:
            dst.write_bytes(payload)

    return node_exe, cli_js


def run_ttfautohint_on_static(in_ttf: Path, out_ttf: Path, spec: Spec) -> float:
    ta_exe, _ = selected_ttfautohint()
    with TTFont(in_ttf, lazy=True) as font:
        cmap = font.getBestCmap() or {}
        gids = sorted({font.getGlyphID(cmap[cp]) for cp in spec.ui_autohint_unscaled if cp in cmap})
    ctrl = out_ttf.with_suffix(".ctrl.txt")
    ctrl.write_text("".join(f"none dflt @ {gid}\n" for gid in gids), encoding="ascii", newline="\n")
    t0 = time.perf_counter()
    cmd = [str(ta_exe), *spec.ui_autohint_options, "--control-file", str(ctrl), str(in_ttf), str(out_ttf)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    ctrl.unlink(missing_ok=True)
    if proc.returncode != 0 or not out_ttf.is_file() or out_ttf.stat().st_size == 0:
        raise RuntimeError(f"ttfautohint 执行失败（rc={proc.returncode}）：{proc.stderr or proc.stdout}")
    return round(time.perf_counter() - t0, 2)


def _load_gzip_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _hint_key_sort_key(key: str) -> tuple[int, str]:
    if "#" in key:
        _prefix, suffix = key.rsplit("#", 1)
        if suffix.isdigit():
            return (int(suffix), key)
    return (1 << 30, key)


def canonicalize_hint_data(data: dict[str, Any]) -> dict[str, Any]:
    """将 .hint.gz 顶层字形与共享提示字典按固定键序重排，消除多线程冷热缓存导致的 fpgm 编号漂移。"""
    out = dict(data)
    if isinstance(out.get("glyphs"), dict):
        out["glyphs"] = {k: out["glyphs"][k] for k in sorted(out["glyphs"], key=_hint_key_sort_key)}
    if isinstance(out.get("glyphHintCacheKeys"), dict):
        out["glyphHintCacheKeys"] = {
            k: out["glyphHintCacheKeys"][k]
            for k in sorted(out["glyphHintCacheKeys"], key=_hint_key_sort_key)
        }
    if isinstance(out.get("sharedHints"), dict):
        out["sharedHints"] = {k: out["sharedHints"][k] for k in sorted(out["sharedHints"])}
    return out


def _save_gzip_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(gzip.compress(payload, compresslevel=6, mtime=0))
    os.replace(tmp, path)


def run_chlorophytum_hint(
    node_exe: Path,
    cli_js: Path,
    hint_env_dir: Path,
    in_ttf: Path,
    out_hgz: Path,
    hcfg_path: Path,
    shared_cache: Path,
    *,
    chloro_jobs: int = 8,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["NODE_PATH"] = str(hint_env_dir / "node_modules")
    work_cache = out_hgz.with_suffix(".workcache.gz")
    if shared_cache.is_file():
        shutil.copy2(shared_cache, work_cache)
    elif work_cache.exists():
        work_cache.unlink()

    t0 = time.perf_counter()
    cmd = [
        str(node_exe),
        str(cli_js),
        "hint",
        "-c", str(hcfg_path),
        "-h", str(work_cache),
        "-j", str(max(1, chloro_jobs)),
        str(in_ttf),
        str(out_hgz),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    sec = round(time.perf_counter() - t0, 2)
    if proc.returncode != 0 or not out_hgz.is_file() or out_hgz.stat().st_size == 0:
        work_cache.unlink(missing_ok=True)
        raise RuntimeError(
            f"Chlorophytum hint 失败（rc={proc.returncode}）：{(proc.stderr or proc.stdout).strip()[:400]}"
        )

    # 规范化 .hint.gz 键序，确保冷缓存多线程与热缓存单序产出的 instruct 字节码逐字节一致
    _save_gzip_json_atomic(out_hgz, canonicalize_hint_data(_load_gzip_json(out_hgz)))

    # 增量合并回共享持久缓存，避免覆盖其他地区的缓存条目，并保持键序确定
    merged = _load_gzip_json(shared_cache)
    before_n = len(merged)
    new_entries = _load_gzip_json(work_cache)
    added = sum(1 for k in new_entries if k not in merged)
    merged.update(new_entries)
    _save_gzip_json_atomic(shared_cache, {k: merged[k] for k in sorted(merged)})
    work_cache.unlink(missing_ok=True)
    return {
        "hint_sec": sec,
        "cache_before": before_n,
        "font_entries": len(new_entries),
        "newly_computed": added,
        "cache_after": len(merged),
    }


def _outline_sig(font: TTFont, gname: str) -> tuple[int, tuple[int, ...], tuple[int, ...], tuple[int, ...]] | None:
    g = font["glyf"][gname]
    g.expand(font["glyf"])
    if g.isComposite() or g.numberOfContours <= 0:
        return None
    ys = tuple(int(y) for _x, y in g.coordinates)
    fl = tuple(int(f) & 1 for f in g.flags)
    ep = tuple(int(e) for e in g.endPtsOfContours)
    return (int(g.numberOfContours), ep, fl, ys)


def _shear_x_compatible(
    fu: TTFont, un: str, fi: TTFont, iname: str, tan_slant: float, tol: float = 2.0
) -> bool:
    gu, gi = fu["glyf"][un], fi["glyf"][iname]
    cu, ci = gu.coordinates, gi.coordinates
    if len(cu) != len(ci):
        return False
    if not cu:
        return True
    # 兼容斜切（9.4°）以及测试中未斜切（0°）的合成字体，要求 x_i - (x_u + y*tan) 为近似常数（otRound 误差 ≤ 1 unit）
    for tan_val in (tan_slant, 0.0):
        diffs = [float(xi) - (float(xu) + float(y) * tan_val) for (xu, y), (xi, _yi) in zip(cu, ci)]
        if max(diffs) - min(diffs) <= tol:
            return True
    return False


def remap_upright_hints_to_italic(
    upright_ttf: Path,
    upright_hgz: Path,
    italic_ttf: Path,
    italic_hgz: Path,
    *,
    slant_deg: float = 9.4,
    allow_skipped: bool = False,
) -> dict[str, int]:
    """严格验证正斜体 CJK/假名/韩文轮廓拓扑与逐点 y 坐标完全一致后，将正体 .hint.gz 精确重映射给斜体。

    斜体静态字形由去重叠后的正体直接按 9.4° 斜切生成（y' == y 严格成立），故此处要求零容差逐点 y 匹配，
    不放宽任何断言；默认严禁静默跳过任何条目。
    """
    data = _load_gzip_json(upright_hgz)
    hinted_keys = set(data.get("glyphs", {}).keys()) | set(data.get("glyphHintCacheKeys", {}).keys())
    tan_slant = math.tan(math.radians(slant_deg))

    with TTFont(upright_ttf, lazy=False) as fu, TTFont(italic_ttf, lazy=False) as fi:
        cu = fu.getBestCmap() or {}
        ci = fi.getBestCmap() or {}
        u_order = fu.getGlyphOrder()
        i_order = fi.getGlyphOrder()
        u_gid_of = {gn: idx for idx, gn in enumerate(u_order)}
        i_gid_of = {gn: idx for idx, gn in enumerate(i_order)}

        u_sigs: dict[int, tuple[int, tuple[int, ...], tuple[int, ...], tuple[int, ...]] | None] = {}
        i_sigs: dict[int, tuple[int, tuple[int, ...], tuple[int, ...], tuple[int, ...]] | None] = {}

        def get_u_sig(gid: int):
            if gid not in u_sigs:
                u_sigs[gid] = _outline_sig(fu, u_order[gid])
            return u_sigs[gid]

        def get_i_sig(gid: int):
            if gid not in i_sigs:
                i_sigs[gid] = _outline_sig(fi, i_order[gid])
            return i_sigs[gid]

        def _exact_sig_compatible(ug: int, ig: int) -> bool:
            su, si = get_u_sig(ug), get_i_sig(ig)
            return (
                su is not None
                and si is not None
                and su == si
                and _shear_x_compatible(fu, u_order[ug], fi, i_order[ig], tan_slant)
            )

        u_to_i_gid: dict[int, int] = {}
        used_i_gids: set[int] = set()
        mismatches: list[str] = []

        # 1. 按 cmap 严格校验并映射全部 CJK/假名/韩文（强断言轮廓数、端点、on-curve 标志、逐点 y 坐标与斜切完全一致）
        for cp in sorted(set(cu) & set(ci)):
            un, iname = cu[cp], ci[cp]
            ug, ig = u_gid_of[un], i_gid_of[iname]
            is_cjk = is_ideograph(cp) or is_kana(cp) or is_korean(cp)
            if is_cjk:
                gu, gi = fu["glyf"][un], fi["glyf"][iname]
                gu.expand(fu["glyf"])
                gi.expand(fi["glyf"])
                if gu.numberOfContours == 0 and gi.numberOfContours == 0:
                    u_to_i_gid[ug] = ig
                    used_i_gids.add(ig)
                    continue
                if not _exact_sig_compatible(ug, ig):
                    mismatches.append(f"U+{cp:04X}")
                    continue
                u_to_i_gid[ug] = ig
                used_i_gids.add(ig)
            elif _exact_sig_compatible(ug, ig) and ug not in u_to_i_gid:
                u_to_i_gid[ug] = ig
                used_i_gids.add(ig)

        if mismatches:
            raise RuntimeError(f"正斜体 CJK 轮廓点数或纵坐标不一致，无法安全复用 hint（{len(mismatches)} 个，样例 {mismatches[:5]}）")

        # 2. 按 cmap format 14 UVS 严格映射变体字形
        u_uvs: dict[tuple[int, int], int] = {}
        i_uvs: dict[tuple[int, int], int] = {}
        for tbl in fu["cmap"].tables:
            if tbl.format == 14:
                for sel, entries in tbl.uvsDict.items():
                    for cp, gn in entries:
                        if gn and gn in u_gid_of:
                            u_uvs[(sel, cp)] = u_gid_of[gn]
        for tbl in fi["cmap"].tables:
            if tbl.format == 14:
                for sel, entries in tbl.uvsDict.items():
                    for cp, gn in entries:
                        if gn and gn in i_gid_of:
                            i_uvs[(sel, cp)] = i_gid_of[gn]
        for pair, ug in u_uvs.items():
            ig = i_uvs.get(pair)
            if ig is None or ug in u_to_i_gid:
                continue
            if _exact_sig_compatible(ug, ig):
                u_to_i_gid[ug] = ig
                used_i_gids.add(ig)

        # 3. 剩余未编码 Chlorophytum 字形（vert/vrt2/locl/Jamo 等）：严格要求相同拓扑、完全一致的 y 序列与斜切 x 校验
        needed_u_gids: list[int] = []
        for k in sorted(hinted_keys):
            if "#" not in k:
                continue
            _prefix, idx_str = k.rsplit("#", 1)
            if idx_str.isdigit():
                ug = int(idx_str)
                if 0 <= ug < len(u_order) and ug not in u_to_i_gid:
                    needed_u_gids.append(ug)

        if needed_u_gids:
            it_by_exact: dict[tuple[int, tuple[int, ...], tuple[int, ...], tuple[int, ...]], list[int]] = {}
            for ig in range(len(i_order)):
                si = get_i_sig(ig)
                if si is not None:
                    it_by_exact.setdefault(si, []).append(ig)
            for ug in needed_u_gids:
                if ug in u_to_i_gid:
                    continue
                su = get_u_sig(ug)
                if su is None:
                    continue
                un = u_order[ug]
                compat = [
                    ig for ig in it_by_exact.get(su, [])
                    if _shear_x_compatible(fu, un, fi, i_order[ig], tan_slant)
                ]
                if not compat:
                    continue
                compat.sort(key=lambda ig: (ig in used_i_gids, abs(ig - ug), ig))
                chosen_ig = compat[0]
                u_to_i_gid[ug] = chosen_ig
                used_i_gids.add(chosen_ig)

        def _remap_key(k: str) -> str | None:
            if "#" not in k:
                return None
            prefix, idx_str = k.rsplit("#", 1)
            if not idx_str.isdigit():
                return None
            ug = int(idx_str)
            ig = u_to_i_gid.get(ug)
            if ig is None:
                return None
            if prefix == f".gid{ug}":
                return f".gid{ig}#{ig}"
            if prefix == ".gid":
                return f".gid#{ig}"
            return f"{prefix}#{ig}"

        new_glyphs = {}
        new_keys = {}
        mapped = skipped = 0
        skipped_samples: list[str] = []
        for k, v in data.get("glyphs", {}).items():
            nk = _remap_key(k)
            if nk:
                new_glyphs[nk] = v
                mapped += 1
            else:
                skipped += 1
                if len(skipped_samples) < 8:
                    skipped_samples.append(k)
        for k, v in data.get("glyphHintCacheKeys", {}).items():
            nk = _remap_key(k)
            if nk:
                new_keys[nk] = v

    if skipped and not allow_skipped:
        raise RuntimeError(
            f"斜体 hint 重映射存在 {skipped} 个无法精确匹配的条目，样例：{skipped_samples}"
        )
    remapped = canonicalize_hint_data({**data, "glyphs": new_glyphs, "glyphHintCacheKeys": new_keys})
    _save_gzip_json_atomic(italic_hgz, remapped)
    return {"italic_hints_mapped": mapped, "italic_hints_skipped": skipped}


def run_chlorophytum_instruct(
    node_exe: Path,
    cli_js: Path,
    hint_env_dir: Path,
    ta_ttf: Path,
    hgz: Path,
    out_ttf: Path,
    hcfg_path: Path,
) -> float:
    env = os.environ.copy()
    env["NODE_PATH"] = str(hint_env_dir / "node_modules")
    t0 = time.perf_counter()
    cmd = [str(node_exe), str(cli_js), "instruct", "-c", str(hcfg_path), str(ta_ttf), str(hgz), str(out_ttf)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0 or not out_ttf.is_file() or out_ttf.stat().st_size == 0:
        raise RuntimeError(
            f"Chlorophytum instruct 失败（rc={proc.returncode}）：{(proc.stderr or proc.stdout).strip()[:400]}"
        )
    return round(time.perf_counter() - t0, 2)


def finalize_hinted_ttf(
    in_ttf: Path,
    out_ttf: Path,
    *,
    region: str,
    weight: WeightSpec,
    italic: bool,
    spec: Spec,
    cvt_min_len: int = DEFAULT_CVT_MIN_LEN,
    unhinted_ttf: Path | None = None,
) -> dict[str, int]:
    """补足 cvt 防止 FreeType 严格执行预算中止、置位 head.flags bit 3、保真 OS/2 并写入确定性 head，原子落盘。"""
    with TTFont(in_ttf, recalcBBoxes=False, recalcTimestamp=False) as font:
        cvt_before = len(font["cvt "].values) if "cvt " in font else 0
        if "cvt " in font:
            vals = list(font["cvt "].values)
            target = max(cvt_min_len, (int(font["maxp"].maxTwilightPoints) + 1) // 2)
            if len(vals) < target:
                vals.extend([0] * (target - len(vals)))
                font["cvt "].values = array.array("h", vals)
        cvt_after = len(font["cvt "].values) if "cvt " in font else 0

        if unhinted_ttf is not None and unhinted_ttf.is_file():
            with TTFont(unhinted_ttf, lazy=True) as ref_un:
                o_dst, o_src = font["OS/2"], ref_un["OS/2"]
                for field in (
                    "ulUnicodeRange1",
                    "ulUnicodeRange2",
                    "ulUnicodeRange3",
                    "ulUnicodeRange4",
                    "ySubscriptXSize",
                    "ySubscriptYSize",
                    "ySubscriptXOffset",
                    "ySubscriptYOffset",
                    "ySuperscriptXSize",
                    "ySuperscriptYSize",
                    "ySuperscriptXOffset",
                    "ySuperscriptYOffset",
                    "sFamilyClass",
                    "sxHeight",
                    "sCapHeight",
                    "usDefaultChar",
                    "usBreakChar",
                    "usWidthClass",
                    "fsType",
                ):
                    if hasattr(o_src, field):
                        setattr(o_dst, field, getattr(o_src, field))

        font["OS/2"].xAvgCharWidth = weight.x_avg_char_width_italic if italic else weight.x_avg_char_width_upright
        # OpenType 规范强烈建议 hinted 字体置位 head.flags bit 3（强制 ppem 取整），与上游 Sarasa Ui（flags=15）一致
        font["head"].flags = (int(font["head"].flags) & ~0x000F) | 0x000F

        update_static_names(font, region, weight, italic, hinted=True, spec=spec)
        font["post"].formatType = 3.0
        font["post"].extraNames = []
        font["post"].mapping = {}
        apply_deterministic_head(font, spec)

        out_ttf.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_ttf.with_suffix(".writing.ttf")
        font.save(tmp, reorderTables=True)
    os.replace(tmp, out_ttf)
    return {"cvt_before": cvt_before, "cvt_after": cvt_after}


def _worker_hint_weight(
    args: tuple[str, str, str, list[str], list[str], str, str, str, int, int]
) -> list[dict[str, Any]]:
    (
        weight_name,
        unhinted_root_str,
        out_root_str,
        regions,
        styles,
        work_root_str,
        hint_env_str,
        cache_dir_str,
        chloro_jobs,
        cvt_min_len,
    ) = args
    spec = load_spec()
    w = spec.weight(weight_name)
    hint_env_dir = Path(hint_env_str)
    node_exe, cli_js = ensure_hint_env(Path(work_root_str), hint_env_dir, spec)
    hcfg_file = w.hcfg if w.hcfg.endswith(".json") else f"{w.hcfg}.json"
    hcfg_path = hint_env_dir / "hcfg" / hcfg_file
    shared_cache = Path(cache_dir_str) / f"cache-{w.name}.gz"
    unhinted_root = Path(unhinted_root_str)
    out_root = Path(out_root_str)

    results: list[dict[str, Any]] = []
    want_upright = "upright" in styles
    want_italic = "italic" in styles

    with tempfile.TemporaryDirectory(prefix=f"sarasa-hint-{w.name}-") as tmp_raw:
        tmp_dir = Path(tmp_raw)
        for region in regions:
            up_src = spec.static_dir(unhinted_root, region, hinted=False) / spec.static_filename(region, w.name, False)
            it_src = spec.static_dir(unhinted_root, region, hinted=False) / spec.static_filename(region, w.name, True)
            up_ta = tmp_dir / f"{region}-{w.name}-up.ta.ttf"
            up_hgz = tmp_dir / f"{region}-{w.name}-up.hint.gz"
            up_raw_out = tmp_dir / f"{region}-{w.name}-up.a1.ttf"

            # 无论是否只要斜体，斜体 CJK 指令都复用自同地区正体，因此正体须先生成 .hint.gz
            if not up_src.is_file():
                raise FileNotFoundError(f"缺少 unhinted 正体输入：{up_src}")
            ta_up_sec = run_ttfautohint_on_static(up_src, up_ta, spec)
            hint_info = run_chlorophytum_hint(
                node_exe, cli_js, hint_env_dir, up_ta, up_hgz, hcfg_path, shared_cache, chloro_jobs=chloro_jobs
            )

            if want_upright:
                inst_sec = run_chlorophytum_instruct(node_exe, cli_js, hint_env_dir, up_ta, up_hgz, up_raw_out, hcfg_path)
                out_up = spec.static_dir(out_root, region, hinted=True) / spec.static_filename(region, w.name, False)
                fin = finalize_hinted_ttf(
                    up_raw_out,
                    out_up,
                    region=region,
                    weight=w,
                    italic=False,
                    spec=spec,
                    cvt_min_len=cvt_min_len,
                    unhinted_ttf=up_src,
                )
                results.append({
                    "region": region,
                    "weight": w.name,
                    "italic": False,
                    "filename": out_up.name,
                    "ta_sec": ta_up_sec,
                    "instruct_sec": inst_sec,
                    **hint_info,
                    **fin,
                })
                print(
                    f"[hinted] {out_up.name}: ta={ta_up_sec}s hint={hint_info['hint_sec']}s "
                    f"(new={hint_info['newly_computed']}/{hint_info['font_entries']}) inst={inst_sec}s",
                    flush=True,
                )

            if want_italic:
                if not it_src.is_file():
                    raise FileNotFoundError(f"缺少 unhinted 斜体输入：{it_src}")
                it_ta = tmp_dir / f"{region}-{w.name}-it.ta.ttf"
                it_hgz = tmp_dir / f"{region}-{w.name}-it.hint.gz"
                it_raw_out = tmp_dir / f"{region}-{w.name}-it.a1.ttf"
                ta_it_sec = run_ttfautohint_on_static(it_src, it_ta, spec)
                remap_info = remap_upright_hints_to_italic(up_ta, up_hgz, it_ta, it_hgz)
                inst_it_sec = run_chlorophytum_instruct(node_exe, cli_js, hint_env_dir, it_ta, it_hgz, it_raw_out, hcfg_path)
                out_it = spec.static_dir(out_root, region, hinted=True) / spec.static_filename(region, w.name, True)
                fin_it = finalize_hinted_ttf(
                    it_raw_out,
                    out_it,
                    region=region,
                    weight=w,
                    italic=True,
                    spec=spec,
                    cvt_min_len=cvt_min_len,
                    unhinted_ttf=it_src,
                )
                results.append({
                    "region": region,
                    "weight": w.name,
                    "italic": True,
                    "filename": out_it.name,
                    "ta_sec": ta_it_sec,
                    "instruct_sec": inst_it_sec,
                    **remap_info,
                    **fin_it,
                })
                print(f"[hinted] {out_it.name}: ta={ta_it_sec}s remap_cjk={remap_info['italic_hints_mapped']} inst={inst_it_sec}s", flush=True)
    return results


def build_hinted_statics(
    unhinted_root: Path,
    out_root: Path,
    regions: list[str],
    styles: list[str],
    weights: list[str],
    *,
    work_root: Path | None = None,
    hint_env_dir: Path | None = None,
    cache_dir: Path | None = None,
    jobs: int = 2,
    chloro_jobs: int = 8,
    cvt_min_len: int = DEFAULT_CVT_MIN_LEN,
    spec: Spec | None = None,
) -> dict[str, Any]:
    """按字重并行、字重内跨地区共享增量缓存地构建混合 hinted 静态字体。"""
    spec = spec or load_spec()
    root = repo_root()
    wroot = (work_root or (root / ".build-cache" / "work")).resolve()
    henv = (hint_env_dir or (root / ".build-cache" / "hint-env")).resolve()
    cdir = (cache_dir or (root / ".build-cache" / "hint-cache")).resolve()
    cdir.mkdir(parents=True, exist_ok=True)

    # 先单进程预检 Node 与 Chlorophytum 环境
    ensure_hint_env(wroot, henv, spec)

    tasks = [
        (
            w_name,
            str(unhinted_root.resolve()),
            str(out_root.resolve()),
            list(regions),
            list(styles),
            str(wroot),
            str(henv),
            str(cdir),
            chloro_jobs,
            cvt_min_len,
        )
        for w_name in weights
    ]
    t0 = time.perf_counter()
    all_results: list[dict[str, Any]] = []
    if jobs <= 1 or len(tasks) <= 1:
        for item in tasks:
            all_results.extend(_worker_hint_weight(item))
    else:
        pool = ProcessPoolExecutor(max_workers=min(jobs, len(tasks)))
        try:
            futs = {pool.submit(_worker_hint_weight, item): item for item in tasks}
            for fut in as_completed(futs):
                all_results.extend(fut.result())
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)

    w_order = {w.name: i for i, w in enumerate(spec.weights)}
    all_results.sort(key=lambda r: (spec.region_order.index(r["region"]), w_order[r["weight"]], r["italic"]))
    summary = {
        "stage": "hinted",
        "count": len(all_results),
        "cvt_min_len": cvt_min_len,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "outputs": all_results,
    }
    (out_root / "hinted-build-report.json").write_bytes(
        (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    return summary
