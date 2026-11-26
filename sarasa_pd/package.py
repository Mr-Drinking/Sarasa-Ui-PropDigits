"""发布打包器：生成 21 个确定性 ZIP、SHA256SUMS.txt 与 release-manifest.json，并提供只读核验。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import time
import zipfile
from typing import Any

from .env import repo_root
from .spec import Spec, load_spec

ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ZIP_EXTERNAL_ATTR = 0o100644 << 16
CHECK_GATE_FILENAME = ".check-passed.json"
REPO_BLOB_BASE = "https://github.com/Mr-Drinking/Sarasa-Ui-PropDigits/blob"
ROOT_DOCUMENTS: tuple[tuple[str, str], ...] = (
    ("LICENSE", "LICENSE.txt"),
    ("NOTICE.md", "NOTICE.md"),
    ("README.md", "README.md"),
)


@dataclass(frozen=True)
class PackagePlan:
    filename: str
    members: tuple[tuple[Path, str], ...]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_replace_path(src: Path, dst: Path, *, retries: int = 8, base_delay: float = 0.25) -> None:
    """带 Windows 共享冲突重试的原子切换。"""
    last_err: OSError | None = None
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except (PermissionError, OSError) as err:
            winerr = getattr(err, "winerror", None)
            if os.name == "nt" and winerr not in {5, 32, 33, 183} and not isinstance(err, PermissionError):
                raise
            last_err = err
            time.sleep(base_delay * (2 ** min(attempt, 4)))
    assert last_err is not None
    raise last_err


def _check_safe_file(path: Path, root: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"缺少打包成员文件：{path}")
    if path.is_symlink() or path.is_junction():
        raise ValueError(f"禁止打包符号链接或联接点：{path.name}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"打包成员越界：{path.name}")


def _root_doc_members(project_root: Path) -> tuple[tuple[Path, str], ...]:
    out: list[tuple[Path, str]] = []
    for src_name, arc_name in ROOT_DOCUMENTS:
        p = project_root / src_name
        _check_safe_file(p, project_root)
        out.append((p, arc_name))
    return tuple(out)


def _variable_members(font_root: Path, spec: Spec, region: str | None = None) -> tuple[tuple[Path, str], ...]:
    vf_dir = font_root / "variable"
    regions = [region] if region else list(spec.region_order)
    out: list[tuple[Path, str]] = []
    for r in regions:
        for italic in (False, True):
            p = vf_dir / spec.variable_filename(r, italic)
            _check_safe_file(p, font_root)
            out.append((p, p.name))
    return tuple(out)


def _static_members(font_root: Path, spec: Spec, region: str, hinted: bool, prefix: str = "") -> tuple[tuple[Path, str], ...]:
    sdir = spec.static_dir(font_root, region, hinted=hinted)
    if not sdir.is_dir():
        raise FileNotFoundError(f"缺少静态字体目录：{sdir}")
    if sdir.is_symlink() or sdir.is_junction():
        raise ValueError(f"静态目录不允许链接：{sdir.name}")

    expected = {
        spec.static_filename(region, w.name, italic)
        for w in spec.weights
        for italic in (False, True)
    }
    actual_paths = sorted(p for p in sdir.iterdir() if p.is_file())
    actual_names = {p.name for p in actual_paths}
    if actual_names != expected:
        raise ValueError(
            f"静态目录成员不精确匹配 12 个 TTF：{sdir.name}；"
            f"缺少 {sorted(expected - actual_names)}，多出 {sorted(actual_names - expected)}"
        )
    out: list[tuple[Path, str]] = []
    for p in actual_paths:
        _check_safe_file(p, sdir)
        out.append((p, f"{prefix}{p.name}"))
    return tuple(out)


def build_package_plans(font_root: Path, spec: Spec, project_root: Path | None = None) -> list[PackagePlan]:
    proot = (project_root or repo_root()).resolve()
    froot = font_root.resolve()
    docs = _root_doc_members(proot)
    ver = spec.version
    plans: list[PackagePlan] = []

    for region in spec.region_order:
        plans.append(
            PackagePlan(
                f"Sarasa-Ui-VF-PropDigits-{region}-TTF-{ver}.zip",
                docs + _variable_members(froot, spec, region),
            )
        )
        plans.append(
            PackagePlan(
                f"SarasaUiPropDigits{region}-TTF-{ver}.zip",
                docs + _static_members(froot, spec, region, hinted=True),
            )
        )
        plans.append(
            PackagePlan(
                f"SarasaUiPropDigits{region}-TTF-Unhinted-{ver}.zip",
                docs + _static_members(froot, spec, region, hinted=False),
            )
        )

    plans.append(
        PackagePlan(
            f"Sarasa-Ui-VF-PropDigits-TTF-{ver}.zip",
            docs + _variable_members(froot, spec),
        )
    )
    for hinted in (True, False):
        variant = "TTF" if hinted else "TTF-Unhinted"
        members: list[tuple[Path, str]] = list(docs)
        for region in spec.region_order:
            sub = spec.static_dirname(region, hinted=hinted)
            members.extend(_static_members(froot, spec, region, hinted=hinted, prefix=f"{sub}/"))
        plans.append(PackagePlan(f"SarasaUiPropDigits-{variant}-{ver}.zip", tuple(members)))

    assert len(plans) == 21, f"期望 21 个发布包，实际 {len(plans)}"
    return plans


def rewrite_readme_for_zip(text: str, version: str) -> str:
    """将 ZIP 内 README.md 的仓库相对链接改写为包内可用路径或对应版本 tag 的 GitHub 链接。"""
    tag_base = f"{REPO_BLOB_BASE}/v{version}"

    def _repl(m: re.Match[str]) -> str:
        label, target = m.group(1), m.group(2)
        if target == "LICENSE":
            return f"[{label}](LICENSE.txt)"
        if target == "NOTICE.md":
            return m.group(0)
        if target.startswith(("docs/", "requirements.")) or target in {"sarasa_pd/spec.toml"}:
            return f"[{label}]({tag_base}/{target})"
        return m.group(0)

    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _repl, text)


def _expected_release_font_files(font_root: Path, spec: Spec) -> list[Path]:
    files: list[Path] = []
    vf_dir = font_root / "variable"
    for region in spec.region_order:
        for italic in (False, True):
            files.append(vf_dir / spec.variable_filename(region, italic))
        for hinted in (True, False):
            sdir = spec.static_dir(font_root, region, hinted=hinted)
            for w in spec.weights:
                for italic in (False, True):
                    files.append(sdir / spec.static_filename(region, w.name, italic))
    return files


def write_check_gate(
    font_root: Path,
    spec: Spec,
    *,
    ft_mode: str,
    skipped_ots: bool,
) -> Path:
    """全量硬门禁通过后在字体根目录写入轻量通过标记，供 package 核对。"""
    froot = font_root.resolve()
    files: dict[str, str] = {}
    for p in _expected_release_font_files(froot, spec):
        if not p.is_file():
            raise FileNotFoundError(f"写入检查标记失败，缺少字体文件：{p}")
        rel = p.relative_to(froot).as_posix()
        files[rel] = sha256_file(p)
    payload = {
        "version": spec.version,
        "ft_mode": ft_mode,
        "ots_verified": not skipped_ots,
        "file_count": len(files),
        "files": files,
    }
    out = froot / CHECK_GATE_FILENAME
    tmp = froot / f"{CHECK_GATE_FILENAME}.writing"
    tmp.write_bytes((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    atomic_replace_path(tmp, out)
    return out


def verify_check_gate(font_root: Path, spec: Spec) -> None:
    """核对字体目录已通过当前版本的全量硬门禁（含 OTS 与 full FreeType 扫描）且字体未被改动。"""
    froot = font_root.resolve()
    gate_path = froot / CHECK_GATE_FILENAME
    if not gate_path.is_file():
        raise ValueError(
            f"缺少全量检查通过标记 {gate_path}；请先运行 "
            f"`python -E -s -X utf8 -m sarasa_pd check --stage all --root {froot} --ft-mode full`"
        )
    data = json.loads(gate_path.read_text(encoding="utf-8"))
    if data.get("version") != spec.version:
        raise ValueError(f"检查通过标记版本不匹配：标记为 {data.get('version')!r}，当前规格为 {spec.version!r}")
    if data.get("ft_mode") != "full" or not data.get("ots_verified"):
        raise ValueError(
            f"打包要求全量硬门禁（ft_mode='full' 且启用 OTS），当前标记为 ft_mode={data.get('ft_mode')!r}, ots_verified={data.get('ots_verified')!r}"
        )
    recorded = data.get("files") or {}
    expected = {p.relative_to(froot).as_posix(): p for p in _expected_release_font_files(froot, spec)}
    if set(recorded) != set(expected):
        raise ValueError("检查通过标记记录的字体列表与当前 156 个发布字体不一致")
    for rel, path in expected.items():
        if not path.is_file():
            raise FileNotFoundError(f"缺少字体文件：{path}")
        if sha256_file(path) != recorded[rel]:
            raise ValueError(f"字体文件在通过检查后发生变化，请重新运行 check：{rel}")


def _write_deterministic_zip(plan: PackagePlan, out_path: Path, version: str) -> dict[str, Any]:
    tmp = out_path.with_suffix(".writing.zip")
    members_meta: list[dict[str, Any]] = []
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for src, arc_name in plan.members:
            if arc_name == "README.md":
                data = rewrite_readme_for_zip(src.read_text(encoding="utf-8"), version).encode("utf-8")
            else:
                data = src.read_bytes()
            info = zipfile.ZipInfo(arc_name, ZIP_TIMESTAMP)
            info.create_system = 3
            info.external_attr = ZIP_EXTERNAL_ATTR
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
            members_meta.append({
                "name": arc_name,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
    atomic_replace_path(tmp, out_path)
    return {
        "filename": plan.filename,
        "size": out_path.stat().st_size,
        "sha256": sha256_file(out_path),
        "member_count": len(members_meta),
        "members": members_meta,
    }


def package_release(
    font_root: Path,
    dist_dir: Path,
    *,
    project_root: Path | None = None,
    require_check_gate: bool = True,
    spec: Spec | None = None,
) -> dict[str, Any]:
    """打包全部 21 个发布 ZIP、SHA256SUMS.txt 与 release-manifest.json，并执行只读核验。"""
    spec = spec or load_spec()
    if require_check_gate:
        verify_check_gate(font_root, spec)
    plans = build_package_plans(font_root, spec, project_root=project_root)
    dist_dir = dist_dir.resolve()
    dist_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    pkg_records = [_write_deterministic_zip(plan, dist_dir / plan.filename, spec.version) for plan in plans]
    pkg_records.sort(key=lambda r: r["filename"])

    sums_lines = [f"{r['sha256']}  {r['filename']}" for r in pkg_records]
    sums_bytes = ("\n".join(sums_lines) + "\n").encode("utf-8")
    sums_tmp = dist_dir / "SHA256SUMS.txt.writing"
    sums_tmp.write_bytes(sums_bytes)
    atomic_replace_path(sums_tmp, dist_dir / "SHA256SUMS.txt")

    manifest = {
        "version": spec.version,
        "tag": f"v{spec.version}",
        "package_count": len(pkg_records),
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "sha256sums_sha256": hashlib.sha256(sums_bytes).hexdigest(),
        "packages": pkg_records,
    }
    man_tmp = dist_dir / "release-manifest.json.writing"
    man_tmp.write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    atomic_replace_path(man_tmp, dist_dir / "release-manifest.json")

    verify_release_packages(dist_dir)
    return manifest


def verify_release_packages(dist_dir: Path) -> dict[str, Any]:
    """只读核验：全部 ZIP 存在、摘要与 SHA256SUMS.txt 和 release-manifest.json 一致、成员完整。"""
    dist_dir = dist_dir.resolve()
    manifest_path = dist_dir / "release-manifest.json"
    sums_path = dist_dir / "SHA256SUMS.txt"
    if not manifest_path.is_file() or not sums_path.is_file():
        raise FileNotFoundError(f"缺少发布清单或 SHA256SUMS.txt：{dist_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    sums_map: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, fname = line.split("  ", 1)
        sums_map[fname] = digest

    expected_files = {p["filename"] for p in manifest["packages"]}
    if set(sums_map) != expected_files:
        raise ValueError("SHA256SUMS.txt 与 release-manifest.json 包列表不一致")

    for pkg in manifest["packages"]:
        fname = pkg["filename"]
        zpath = dist_dir / fname
        if not zpath.is_file():
            raise FileNotFoundError(f"缺少 ZIP 包：{fname}")
        actual_sha = sha256_file(zpath)
        if actual_sha != pkg["sha256"] or actual_sha != sums_map[fname]:
            raise ValueError(f"ZIP 摘要不匹配：{fname}")
        with zipfile.ZipFile(zpath, "r") as zf:
            names = zf.namelist()
            if names != [m["name"] for m in pkg["members"]]:
                raise ValueError(f"ZIP 成员列表不匹配：{fname}")
            for m in pkg["members"]:
                if m["name"].endswith(("README.txt", "LICENSE-Sarasa-Gothic.txt")):
                    raise ValueError(f"发现应移除的旧重复文档：{fname}::{m['name']}")
                data = zf.read(m["name"])
                if hashlib.sha256(data).hexdigest() != m["sha256"]:
                    raise ValueError(f"ZIP 内部成员损坏：{fname}::{m['name']}")
    return {"verified_packages": len(manifest["packages"]), "version": manifest["version"]}
