from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import hashlib
import json
import os
import re
import subprocess
import sys
import venv
from pathlib import Path
from typing import Mapping


Dependency = tuple[str, str, str]

BUILD_DEPENDENCIES = {
    "fontTools": ("fonttools", "fonttools[woff]==4.63.0", "4.63.0"),
    "uharfbuzz": ("uharfbuzz", "uharfbuzz==0.56.0", "0.56.0"),
    "freetype": ("freetype-py", "freetype-py==2.5.1", "2.5.1"),
    "brotli": ("Brotli", "brotli==1.2.0", "1.2.0"),
    "ttfautohint": ("ttfautohint-py", "ttfautohint-py==0.6.1", "0.6.1"),
    "py7zr": ("py7zr", "py7zr==1.1.3", "1.1.3"),
    "afdko": ("afdko", "afdko==5.0.1", "5.0.1"),
    "chws_tool": ("chws-tool", "chws-tool==1.4.5", "1.4.5"),
    "east_asian_spacing": ("east-asian-spacing", "east-asian-spacing==1.4.5", "1.4.5"),
}
AUDIT_DEPENDENCIES = {**BUILD_DEPENDENCIES, "numpy": ("numpy", "numpy==2.4.2", "2.4.2")}
VISUAL_DEPENDENCIES = {**AUDIT_DEPENDENCIES, "PIL": ("Pillow", "Pillow==12.3.0", "12.3.0")}


def missing_dependencies(dependencies: Mapping[str, Dependency]) -> list[str]:
    needed: list[str] = []
    for module, (distribution, package_spec, expected_version) in dependencies.items():
        try:
            installed_version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            installed_version = None
        if importlib.util.find_spec(module) is None or installed_version != expected_version:
            needed.append(package_spec)
    return needed


def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def locked_requirements(path: Path) -> dict[str, str]:
    # pip is present in newly created environments, even before packaging has
    # been installed as a project dependency.
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name
    result = {}
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        pending += line.removesuffix("\\").strip() + " "
        if continued:
            continue
        hashes = re.findall(r"--hash=sha256:[a-f0-9]{64}(?=\s|$)", pending)
        if not hashes:
            raise ValueError("锁文件中的每个依赖都必须包含 SHA-256")
        requirement = Requirement(re.sub(r"--hash=sha256:[a-f0-9]{64}", "", pending).strip())
        pending = ""
        specifiers = list(requirement.specifier)
        if requirement.url or len(specifiers) != 1 or specifiers[0].operator != "==" or "*" in specifiers[0].version:
            raise ValueError("锁文件必须使用精确依赖版本")
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        name = canonicalize_name(requirement.name)
        version = specifiers[0].version
        if name in result and result[name] != version:
            raise ValueError("锁文件在当前平台存在版本冲突")
        result[name] = version
    if pending or not result:
        raise ValueError("锁文件不完整")
    return result


def locked_environment_status(path: Path) -> dict[str, object]:
    expected = locked_requirements(path)
    actual = {}
    for name in expected:
        try:
            actual[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual[name] = None
    return {"ok": actual == expected, "expected": expected, "actual": actual,
            "mismatches": [name for name in expected if actual[name] != expected[name]]}


def dependency_lock_path(project_root: Path, dependencies: Mapping[str, Dependency]) -> Path:
    scope = "visual" if "PIL" in dependencies else "audit" if "numpy" in dependencies else "build"
    path = project_root / f"requirements-{scope}.lock"
    if not path.is_file():
        raise FileNotFoundError(f"缺少完整依赖锁文件：{path.name}")
    return path


def ensure_project_python(
    dependencies: Mapping[str, Dependency],
    *,
    project_root: Path,
    label: str,
) -> None:
    if os.environ.get("SARASA_SKIP_PYTHON_DEPS") == "1":
        return
    venv_dir = Path(
        os.environ.get(
            "SARASA_PYTHON_VENV",
            project_root / ".build-cache" / "build-audit-venv",
        )
    ).resolve()
    python = venv_python(venv_dir)
    # Symlinked venv executables can resolve to the same base Python. The
    # environment prefix, not the executable or an inherited marker, owns pip.
    active = Path(sys.prefix).resolve() == venv_dir
    if not active:
        if not python.exists():
            print(f"[{label}] create project Python environment: {venv_dir}", flush=True)
            venv.EnvBuilder(with_pip=True, clear=False).create(venv_dir)
        env = dict(os.environ)
        env["SARASA_PYTHON_ENV_ACTIVE"] = "1"
        original_args = list(getattr(sys, "orig_argv", [sys.executable, *sys.argv]))
        argv = [str(python), *original_args[1:]]
        if os.name == "nt":
            # Windows has no POSIX exec; preserve redirected handles and quote
            # the argument vector through subprocess instead of the CRT overlay.
            raise SystemExit(subprocess.call(argv, env=env))
        os.execve(str(python), argv, env)

    lockfile = dependency_lock_path(project_root, dependencies)
    needed = missing_dependencies(dependencies)
    locked = locked_environment_status(lockfile)
    if not needed and locked["ok"]:
        subprocess.check_call([sys.executable, "-m", "pip", "check"])
        return

    print(
        f"[{label}] install complete hash-locked dependencies from {lockfile.name}",
        flush=True,
    )
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--require-hashes",
            "-r",
            str(lockfile),
        ]
    )
    importlib.invalidate_caches()
    remaining = missing_dependencies(dependencies)
    locked = locked_environment_status(lockfile)
    if remaining or not locked["ok"]:
        raise RuntimeError(
            "Python dependencies remain unsatisfied after project-environment install: "
            + " ".join([*remaining, *locked["mismatches"]])
        )
    subprocess.check_call([sys.executable, "-m", "pip", "check"])


def isolated_tool_python(
    directory: Path,
    requirements: Path,
    versions: Mapping[str, str],
) -> tuple[Path, dict[str, object]]:
    """Prepare a separate tool environment without restarting the caller."""
    directory = directory.resolve()
    if directory == Path(sys.prefix).resolve():
        raise RuntimeError("FontBakery 必须使用独立于构建和主审计的 Python 环境")
    python = venv_python(directory)
    if not python.exists():
        venv.EnvBuilder(with_pip=True, clear=False).create(directory)
    probe = (
        "import json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
        "import python_env_bootstrap as b; print(json.dumps(b.locked_environment_status(Path(sys.argv[2]))))"
    )
    def installed() -> dict[str, object]:
        result = subprocess.run([str(python), "-c", probe, str(Path(__file__).resolve().parent), str(requirements)], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    actual = installed()
    if not actual["ok"]:
        subprocess.check_call([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--require-hashes", "-r", str(requirements)])
        actual = installed()
    selected = {name: actual["actual"].get(name) for name in versions}
    if not actual["ok"] or selected != dict(versions):
        raise RuntimeError("独立发布工具环境未满足固定版本")
    checked = subprocess.run([str(python), "-m", "pip", "check"], capture_output=True, text=True)
    if checked.returncode:
        raise RuntimeError("独立发布工具环境存在依赖冲突：" + checked.stdout + checked.stderr)
    return python, {"versions": selected, "locked_packages": actual["actual"],
                    "lockfile": requirements.name, "lockfile_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
                    "require_hashes": True, "pip_check": {"returncode": 0, "passed": True}}
