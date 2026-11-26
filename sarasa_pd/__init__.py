"""Sarasa Ui PropDigits 构建与验证包。"""
from __future__ import annotations

from .spec import load_spec

__all__ = ["__version__"]
__version__ = load_spec().version
