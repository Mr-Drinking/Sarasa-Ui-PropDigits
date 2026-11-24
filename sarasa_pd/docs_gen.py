"""由 spec.toml 生成文档中的规格表格，并在测试中校验文档与规格一致。"""
from __future__ import annotations

import re
from pathlib import Path

from .spec import Spec, load_spec

_BLOCK_RE = re.compile(
    r"(<!-- BEGIN SPEC:([a-z0-9_]+) -->\n)(.*?)(\n<!-- END SPEC:\2 -->)",
    re.DOTALL,
)


def render_regions_table(spec: Spec) -> str:
    lines = [
        "| 地区 | CJK 字形来源 | 本地化家族名 | OS/2 码页范围 1 |",
        "| --- | --- | --- | --- |",
    ]
    for code in spec.region_order:
        r = spec.region(code)
        src = (
            f"{Path(r.source_han_vf).stem} 基底，叠加 {Path(r.classical_vf).stem}（传统旧字形与 CJK 全宽破折号）"
            if r.classical and r.classical_vf
            else Path(r.source_han_vf).stem
        )
        lines.append(f"| {r.code} | {src} | `{r.local_family} Ui PD {r.code}` | `0x{r.codepage_range1:08X}` |")
    return "\n".join(lines)


def render_weights_table(spec: Spec) -> str:
    lines = [
        "| 公开坐标 | 样式 | Source Han 内部坐标 | Inter 坐标 | 下划线粗/位 | 删除线粗 | hcfg 配置 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for w in spec.weights:
        shs_w = spec.public_to_internal_wght[w.value]
        hcfg_file = w.hcfg if w.hcfg.endswith(".json") else f"{w.hcfg}.json"
        lines.append(
            f"| {w.value} | {w.name} | {shs_w} | {w.value} | {w.underline_thickness} / {w.underline_position} | {w.strikeout_size} | `{hcfg_file}` |"
        )
    return "\n".join(lines)


def render_differences_table(spec: Spec, *, kind: str | None = None) -> str:
    lines = [
        "| 编号 | 类别 | 条目 | 依据 |",
        "| --- | --- | --- | --- |",
    ]
    kind_labels = {
        "intentional": "有意差异",
        "source_vf": "跟随 VF 来源",
        "upstream_inherited": "继承上游",
    }
    for d in spec.differences:
        if kind is not None and d.kind != kind:
            continue
        lines.append(f"| `{d.id}` | {kind_labels.get(d.kind, d.kind)} | {d.title} | {d.basis} |")
    return "\n".join(lines)


def render_sources_table(spec: Spec) -> str:
    lines = [
        "| 来源 | 归档文件 | SHA-256（前 16 位） |",
        "| --- | --- | --- |",
    ]
    for key in ("source_han_vf", "inter", "shanggu_vf", "sarasa_ui_unhinted_all"):
        pin = spec.archives[key]
        lines.append(f"| `{key}` | `{pin.filename}` | `{pin.sha256[:16]}…` |")
    return "\n".join(lines)


def render_spec_block(name: str, spec: Spec | None = None) -> str:
    spec = spec or load_spec()
    builders = {
        "regions": render_regions_table,
        "weights": render_weights_table,
        "differences": render_differences_table,
        "differences_intentional": lambda s: render_differences_table(s, kind="intentional"),
        "differences_source_vf": lambda s: render_differences_table(s, kind="source_vf"),
        "differences_upstream": lambda s: render_differences_table(s, kind="upstream_inherited"),
        "sources": render_sources_table,
    }
    if name not in builders:
        raise KeyError(f"未知规格文档块：{name!r}")
    return builders[name](spec)


def sync_doc_text(text: str, spec: Spec | None = None) -> str:
    spec = spec or load_spec()

    def repl(match: re.Match[str]) -> str:
        head, name, _body, tail = match.groups()
        return head + render_spec_block(name, spec) + tail

    return _BLOCK_RE.sub(repl, text)


def check_doc_files(paths: list[Path], spec: Spec | None = None) -> list[str]:
    """核验文档中全部 SPEC 标记块与 spec.toml 一致；返回不一致列表。"""
    spec = spec or load_spec()
    mismatches: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8")
        expected = sync_doc_text(raw, spec)
        if raw != expected:
            mismatches.append(path.name)
    return mismatches
