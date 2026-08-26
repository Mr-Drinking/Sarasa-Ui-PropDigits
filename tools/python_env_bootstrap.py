from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
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
    if not needed:
        return

    venv_dir = Path(
        os.environ.get(
            "SARASA_PYTHON_VENV",
            project_root / ".build-cache" / "python-venv",
        )
    ).resolve()
    python = venv_python(venv_dir)
    active = (
        os.environ.get("SARASA_PYTHON_ENV_ACTIVE") == "1"
        or (python.exists() and Path(sys.executable).resolve() == python.resolve())
    )
    if not active:
        if not python.exists():
            print(f"[{label}] create project Python environment: {venv_dir}", flush=True)
            venv.EnvBuilder(with_pip=True, clear=False).create(venv_dir)
        env = dict(os.environ)
        env["SARASA_PYTHON_ENV_ACTIVE"] = "1"
        original_args = list(getattr(sys, "orig_argv", [sys.executable, *sys.argv]))
        os.execve(str(python), [str(python), *original_args[1:]], env)

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
