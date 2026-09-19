from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
import venv
from pathlib import Path
from typing import Mapping


Dependency = tuple[str, str, str]


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


def ensure_project_python(
    dependencies: Mapping[str, Dependency],
    *,
    project_root: Path,
    label: str,
) -> None:
    if os.environ.get("SARASA_SKIP_PYTHON_DEPS") == "1":
        return
    needed = missing_dependencies(dependencies)
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

    if not needed:
        subprocess.check_call([sys.executable, "-m", "pip", "check"])
        return

    print(
        f"[{label}] install pinned dependencies in project environment: {' '.join(needed)}",
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
            *needed,
        ]
    )
    importlib.invalidate_caches()
    remaining = missing_dependencies(dependencies)
    if remaining:
        raise RuntimeError(
            "Python dependencies remain unsatisfied after project-environment install: "
            + " ".join(remaining)
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
        "import importlib.metadata as m,json,sys; "
        "print(json.dumps({n:m.version(n) for n in sys.argv[1:]}))"
    )
    def installed() -> dict[str, str] | None:
        result = subprocess.run([str(python), "-c", probe, *versions], capture_output=True, text=True)
        return json.loads(result.stdout) if result.returncode == 0 else None
    actual = installed()
    if actual != dict(versions):
        subprocess.check_call([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-r", str(requirements)])
        actual = installed()
    if actual != dict(versions):
        raise RuntimeError("独立发布工具环境未满足固定版本")
    checked = subprocess.run([str(python), "-m", "pip", "check"], capture_output=True, text=True)
    if checked.returncode:
        raise RuntimeError("独立发布工具环境存在依赖冲突：" + checked.stdout + checked.stderr)
    return python, {"versions": actual, "pip_check": {"returncode": 0, "passed": True}}
