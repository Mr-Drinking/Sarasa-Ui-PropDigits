"""由 spec.toml 生成文档中的规格表格，并核对文档与规格一致。

README.md 与 docs/*.md 用各占一行的 ``<!-- BEGIN SPEC:名称 -->`` 与 ``<!-- END SPEC:名称 -->``
圈出表格位置，两行之间的内容由 ``python -m sarasa_pd docs`` 按 spec.toml 生成（``--check`` 只核对），
单元测试核对文档与规格一致。
"""
from __future__ import annotations

from collections.abc import Callable
import re
from pathlib import Path

from .spec import Spec, load_spec

# 规格块：BEGIN 与 END 各占一行、名称相同；两行之间可以为空（表格尚未生成）。
_BLOCK_RE = re.compile(
    r"^<!-- BEGIN SPEC:([a-z0-9_]+) -->\n(.*?)^<!-- END SPEC:\1 -->$",
    re.DOTALL | re.MULTILINE,
)
# 行首像标记的行。数目不是规格块的两倍，说明有写错、缺配对或嵌套而没被识别的标记。
_MARKER_LINE_RE = re.compile(r"^[ \t]*<!--[ \t]*(?:BEGIN|END)[ \t]+SPEC\b", re.MULTILINE)


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
    """差异清单。指定 kind 时只列这一类，所在章节已写明类别，表里不再重复类别一栏。"""
    kind_labels = {
        "intentional": "有意差异",
        "source_vf": "跟随 VF 来源",
        "upstream_inherited": "继承上游",
    }
    if kind is None:
        lines = ["| 编号 | 类别 | 条目 | 依据 |", "| --- | --- | --- | --- |"]
    else:
        lines = ["| 编号 | 条目 | 依据 |", "| --- | --- | --- |"]
    for d in spec.differences:
        if kind is None:
            lines.append(f"| `{d.id}` | {kind_labels.get(d.kind, d.kind)} | {d.title} | {d.basis} |")
        elif d.kind == kind:
            lines.append(f"| `{d.id}` | {d.title} | {d.basis} |")
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


_BUILDERS: dict[str, Callable[[Spec], str]] = {
    "regions": render_regions_table,
    "weights": render_weights_table,
    "differences": render_differences_table,
    "differences_intentional": lambda s: render_differences_table(s, kind="intentional"),
    "differences_source_vf": lambda s: render_differences_table(s, kind="source_vf"),
    "differences_upstream": lambda s: render_differences_table(s, kind="upstream_inherited"),
    "sources": render_sources_table,
}


def render_spec_block(name: str, spec: Spec | None = None) -> str:
    if name not in _BUILDERS:
        raise KeyError(f"未知规格文档块：{name!r}")
    return _BUILDERS[name](spec or load_spec())


def doc_paths(root: Path) -> list[Path]:
    """要同步与核对的文档：仓库根目录的 README.md 与 docs/ 下全部 Markdown。"""
    return [root / "README.md", *sorted((root / "docs").glob("*.md"))]


def _render_block(name: str, spec: Spec) -> str:
    return f"<!-- BEGIN SPEC:{name} -->\n{render_spec_block(name, spec)}\n<!-- END SPEC:{name} -->"


def _marker_problems(text: str, blocks: list[re.Match[str]]) -> list[str]:
    problems: list[str] = []
    if len(_MARKER_LINE_RE.findall(text)) != 2 * len(blocks):
        problems.append("有无法识别的 SPEC 标记（BEGIN 与 END 须各占一行、名称相同、成对且不嵌套）")
    unknown = sorted({m.group(1) for m in blocks} - _BUILDERS.keys())
    if unknown:
        problems.append("未知的规格块：" + "、".join(unknown))
    return problems


def sync_doc_text(text: str, spec: Spec | None = None) -> str:
    """把每个规格块的内容换成按 spec.toml 生成的表格；标记有误时抛出 ValueError，不做部分替换。"""
    spec = spec or load_spec()
    blocks = list(_BLOCK_RE.finditer(text))
    problems = _marker_problems(text, blocks)
    if problems:
        raise ValueError("；".join(problems))
    return _BLOCK_RE.sub(lambda m: _render_block(m.group(1), spec), text)


def check_doc_files(paths: list[Path], spec: Spec | None = None) -> list[str]:
    """核对文档中全部规格块与 spec.toml 一致；返回问题列表（每项以文件名开头），全部一致时为空。"""
    spec = spec or load_spec()
    problems: list[str] = []
    for path in paths:
        if not path.is_file():
            problems.append(f"{path.name}：文件不存在")
            continue
        text = path.read_text(encoding="utf-8")
        blocks = list(_BLOCK_RE.finditer(text))
        marker_problems = _marker_problems(text, blocks)
        if marker_problems:
            problems.extend(f"{path.name}：{p}" for p in marker_problems)
            continue
        stale = [m.group(1) for m in blocks if m.group(0) != _render_block(m.group(1), spec)]
        if stale:
            problems.append(
                f"{path.name}：规格表与 spec.toml 不一致（{'、'.join(stale)}），运行 python -m sarasa_pd docs 同步"
            )
    return problems


def sync_doc_files(paths: list[Path], spec: Spec | None = None) -> list[Path]:
    """按 spec.toml 重写文档中的规格块，返回内容有变化的文件；任一文件有问题时抛出 ValueError，一个也不写。"""
    spec = spec or load_spec()
    errors: list[str] = []
    updates: list[tuple[Path, str]] = []
    for path in paths:
        if not path.is_file():
            errors.append(f"{path.name}：文件不存在")
            continue
        text = path.read_text(encoding="utf-8")
        try:
            synced = sync_doc_text(text, spec)
        except ValueError as err:
            errors.append(f"{path.name}：{err}")
            continue
        if synced != text:
            updates.append((path, synced))
    if errors:
        raise ValueError("\n".join(errors))
    for path, synced in updates:
        # 按 UTF-8 字节写入：Windows 文本模式会把 LF 写成 CRLF
        path.write_bytes(synced.encode("utf-8"))
    return [path for path, _ in updates]
