"""环境与依赖核验：支持 Python 3.13+，核对锁文件版本并阻止 PYTHONPATH 阴影副本。"""
from __future__ import annotations

from dataclasses import dataclass
import importlib.metadata
import importlib.util
import os
from pathlib import Path
import re
import shutil
import sys


MIN_PYTHON = (3, 13)
# 顶层导入模块 -> 锁文件中的规范化发行包名
CORE_MODULES: dict[str, str] = {
    "fontTools": "fonttools",
    "uharfbuzz": "uharfbuzz",
    "freetype": "freetype-py",
    "brotli": "brotli",
    "ttfautohint": "ttfautohint-py",
    "py7zr": "py7zr",
    "chws_tool": "chws-tool",
    "east_asian_spacing": "east-asian-spacing",
    "PIL": "pillow",
    "pathops": "skia-pathops",
}


@dataclass(frozen=True)
class EnvCheckResult:
    ok: bool
    python_version: str
    isolated: bool
    errors: tuple[str, ...]
    versions: dict[str, str]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_lockfile(lock_path: Path) -> dict[str, str]:
    """解析 uv/pip 生成的哈希锁文件，返回当前平台适用的包名 -> 精确版本。"""
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name

    result: dict[str, str] = {}
    pending = ""
    for raw in lock_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        pending += line.removesuffix("\\").strip() + " "
        if continued:
            continue
        hashes = re.findall(r"--hash=sha256:[a-f0-9]{64}(?=\s|$)", pending)
        if not hashes:
            raise ValueError(f"锁文件条目缺少 SHA-256：{pending.strip()}")
        cleaned = re.sub(r"--hash=sha256:[a-f0-9]{64}", "", pending).strip()
        pending = ""
        req = Requirement(cleaned)
        specs = list(req.specifier)
        if req.url or len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
            raise ValueError(f"锁文件必须使用精确版本：{cleaned}")
        if req.marker is not None and not req.marker.evaluate():
            continue
        name = canonicalize_name(req.name)
        version = specs[0].version
        if name in result and result[name] != version:
            raise ValueError(f"锁文件存在版本冲突：{name}")
        result[name] = version
    if pending or not result:
        raise ValueError(f"锁文件不完整：{lock_path}")
    return result


def _module_within_distribution(module_name: str, dist: importlib.metadata.Distribution) -> tuple[bool, str]:
    spec = importlib.util.find_spec(module_name)
    if spec is None:
        return False, f"找不到模块 {module_name}"
    origin = spec.origin
    if not origin or origin in {"built-in", "frozen"}:
        return False, f"模块 {module_name} 来源异常：{origin!r}"
    origin_path = Path(origin).resolve()
    dist_root = Path(str(dist.locate_file(""))).resolve()
    try:
        origin_path.relative_to(dist_root)
    except ValueError:
        return (
            False,
            f"模块 {module_name} 实际从 {origin_path} 导入，不在已安装包目录 {dist_root} 内（可能被 PYTHONPATH 覆盖）",
        )
    return True, str(origin_path)


def check_environment(lock_path: Path | None = None) -> EnvCheckResult:
    errors: list[str] = []
    versions: dict[str, str] = {}
    py_ver = ".".join(str(x) for x in sys.version_info[:3])

    if sys.version_info[:2] < MIN_PYTHON:
        errors.append(f"需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+，当前为 {py_ver}")

    lock = lock_path or (repo_root() / "requirements.lock")
    if not lock.is_file():
        errors.append(f"缺少依赖锁文件：{lock}")
        return EnvCheckResult(
            ok=False,
            python_version=py_ver,
            isolated=bool(sys.flags.isolated),
            errors=tuple(errors),
            versions=versions,
        )

    expected = parse_lockfile(lock)
    for dist_name, expected_ver in sorted(expected.items()):
        try:
            actual_ver = importlib.metadata.version(dist_name)
        except importlib.metadata.PackageNotFoundError:
            errors.append(f"缺少锁定依赖：{dist_name}=={expected_ver}")
            continue
        versions[dist_name] = actual_ver
        if actual_ver != expected_ver:
            errors.append(f"依赖版本不匹配：{dist_name} 实际 {actual_ver}，期望 {expected_ver}")

    for mod_name, dist_name in CORE_MODULES.items():
        try:
            dist = importlib.metadata.distribution(dist_name)
        except importlib.metadata.PackageNotFoundError:
            continue
        ok, detail = _module_within_distribution(mod_name, dist)
        if not ok:
            errors.append(detail)

    return EnvCheckResult(
        ok=not errors,
        python_version=py_ver,
        isolated=bool(sys.flags.isolated),
        errors=tuple(errors),
        versions=versions,
    )


def selected_ttfautohint() -> tuple[Path, str]:
    """返回锁定的 ttfautohint-py 可执行文件，或显式 TTFAUTOHINT 环境变量覆盖。"""
    override = os.environ.get("TTFAUTOHINT")
    if override:
        path = Path(shutil.which(override) or override).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"指定的 ttfautohint 不存在：{path}")
        return path, "external"
    import ttfautohint

    return Path(ttfautohint._executable_path()).resolve(), "ttfautohint-py"


def head_modified_timestamp(default_timestamp: int) -> int:
    """支持 SOURCE_DATE_EPOCH 覆盖的确定性时间戳。"""
    raw = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if raw:
        return int(raw)
    return default_timestamp
