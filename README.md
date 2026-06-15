# 更纱黑体 Ui PropDigits SC

## 为什么要制作它？

曾几何时，我试图使用[更纱黑体（Sarasa Gothic）](https://github.com/be5invis/sarasa-gothic)作为 Android 手机的默认字体（因为个人更喜欢 [SF Pro](https://developer.apple.com/cn/fonts/) / [Inter](https://rsms.me/inter/) 而非 [Roboto](https://fonts.google.com/specimen/Roboto)），但由于修改软件只支持一个字体，我需要一个可变字体。但可惜的是，[原版更纱并不支持](https://github.com/be5invis/Sarasa-Gothic/issues/314)，我只好自己动手，用 Codex 中的 GPT-5.5 制作了本字体的可变版本；静态版本以及比例宽数字则是源于我对其成为 [Unigram](https://github.com/unigramdev/unigram) / Windows 默认字体的需要。

## 简介

这个仓库包含两个 Sarasa Ui SC 派生字体系列：

- **Sarasa Ui VF PropDigits SC**：正体和 Italic 可变字体，公开 `wght` 轴为 `200..900`。
- **Sarasa Ui PropDigits SC**：从静态 Source Han Sans SC 和 Inter 按 Sarasa 静态片段路径构建的 TTF，包含 hinted 与 unhinted 两套，每套 7 个字重及对应 Italic。

两个系列都把 ASCII 数字 `U+0030..U+0039` 设为默认变宽数字，并提供 OpenType `tnum`/`pnum` 在变宽数字和等宽数字之间切换。VF 与静态 TTF 都按 Inter 相关 `calt` 行为处理冒号：`1:2` 会上浮，`1:a`、`a:2`、`a:b` 不会上浮，`1::2`、`1:::a`、`a:::2` 等冒号串遵循 Inter 的 colon-run 规则。

## 文件结构

```text
fonts/
  variable/
    Sarasa-Ui-VF-PropDigits-SC[wght].ttf
    Sarasa-Ui-VF-PropDigits-SC-Italic[wght].ttf
  static/
    SarasaUiPropDigitsSC-TTF-1.0.39/
      SarasaUiPropDigitsSC-*.ttf
      README.txt
      LICENSE-Sarasa-Gothic.txt
    SarasaUiPropDigitsSC-TTF-Unhinted-1.0.39/
      SarasaUiPropDigitsSC-*.ttf
      README.txt
      LICENSE-Sarasa-Gothic.txt
reports/
  Sarasa-Ui-VF-PropDigits-SC-report.json
  font-inspection.json
tools/
  build_sarasa_ui_propdigits_sc.py
```

## 构建逻辑

VF 不从静态字重插值生成。它直接合并：

- `SourceHanSansSC-VF.ttf`
- `InterVariable.ttf`
- `InterVariable-Italic.woff2`

构建时对齐 Sarasa Ui 的处理方式：

- VF 的公开 `wght` 轴是 `200..900`，默认值 `400`。Source Han Sans SC VF
  内部仍按 `250..900` 裁剪并参与插值；最终 `avar` 把 public `200` 映射到
  Source Han 内部 `250`，其余公开实例尽量映射到同数字上游位置。Inter VF
  则直接按 public `200..900` 裁剪。
- Inter 先烘焙 Sarasa 原版给 Inter 配置的 `ss03` 和 `cv10`。
- 码位归属遵循 Sarasa pass1 的优先级，并按 VF 源文件实际覆盖做兜底：Latin 和西文符号优先来自 Inter VF；CJK、Hangul、Jamo 和 Sarasa Ui 的本地化标点优先来自 Source Han Sans SC VF。
- Source Han 侧烘焙 Ui 标点需要的 `pwid` 替换，并执行 Sarasa 式符号清洗，例如 `·`、弯引号、短横、省略号、`⸺/⸻` 和注音扩展符号宽度处理。
- Hangul / Jamo 宽度归一到全角。
- 最终 GSUB 保留上游 Sarasa Ui 有的 `ccmp`，并保留裁剪到上游覆盖范围的 `locl`、Hangul Jamo、`vert` `vrt2`、`tnum` `pnum`、连续 em dash 和与 Inter 兼容的冒号 `calt`。
- 最终 `GSUB`/`GPOS` 的 FeatureRecord 顺序、Script / LangSys 覆盖和基础 lookup 结构按对应样式的上游 Sarasa Ui SC 静态字体套模板，避免只有 default langsys 而缺少 `JAN`/`KOR`/`ZHH`/`ZHS`/`ZHT`、Latin `CAT`/`MOL`/`ROM` 等语言系统。VF 与静态 TTF 都会移除过宽或过窄的既有冒号上下文替换，再追加一组与 Inter shaping 样例一致的 colon-run lookup。
- VF、hinted 静态 TTF 和 unhinted 静态 TTF 都包含 `STAT`。VF 的 `STAT` 描述 `wght`/`ital` 轴和命名实例；静态 TTF 的 `STAT` 只用于现代应用识别 weight / italic 样式，不表示静态文件仍有 `fvar` `gvar` 可变轴。
- `name` 表包含简体中文显示名：静态为 `更纱黑体 Ui PropDigits SC`，VF 为 `更纱黑体 Ui VF PropDigits SC`。
- 构建会按上游 Sarasa Ui SC 同步非数字与非冒号 advance、横向 LSB、垂直指标、`GDEF`、`VORG`、`vmtx`、`head`/`OS/2` 中可安全继承的元数据字段；静态 exact 样式还会保留上游 simple glyph flags、glyf bbox 和组合字形的组件名。数字和与 Inter 兼容的冒号上下文是本派生字体的刻意差异。
- 静态 TTF 不从 VF 实例化。hinted 和 unhinted 两套都使用静态 Source Han Sans SC 与静态 Inter，按 Sarasa 上游的 `pass1`、`kanji`、`hangul`、`pass2` 片段流程构建；最终 TTF 将默认数字和 `:` remap 到已有的 pnum glyph，清理旧冒号上下文替换后追加与 Inter shaping 样例一致的 colon-run `calt`，中文名、metadata、glyf flags / bbox、组件名和静态 `STAT` 也在最终 TTF 上同步。
- hinted 静态 TTF 与上游 Sarasa 的顺序保持一致：`pass1` 先经过 `ttfautohint`，随后 `pass1`/`kanji`/`hangul` 片段用同版本 Chlorophytum `hcfg` 写入 TrueType 指令，最后由 `pass2` 合成。对于官方存在且轮廓相同的 exact 样式，最终 TTF 还会同步官方 TrueType instruction tables 和同名 glyph 的 program；官方没有的 `Normal`/`Medium`/`Heavy` 静态字重分别使用上游 `Regular`/`SemiBold`/`Bold` 的构建样式或 hint 配置。冒号上浮使用现有片段里的 raised glyph，缺失时才在最终 TTF 上补齐。
- unhinted 静态 TTF 使用相同的静态片段路径，但跳过 `ttfautohint` 和 Chlorophytum，直接由未 hint 的 `pass1`/`kanji`/`hangul` 合成；这是一套正式输出，供需要无 TrueType instructions 版本的使用场景选择。
- 静态 TTF 使用 `post` format 2 保存 glyph names；这是为了在默认比例数字改挂到 U+0030..U+0039 后，`glyph01332`/`glyph01334` 这类上游组件名仍能在保存、重开和审计中稳定保留。VF 仍保持原来的 `post`/GID 模型。
- glyph 总数不作为构建目标。脚本会保留和同步 cmap 字形以及 GSUB/GPOS/GDEF 可达的未编码 glyph；不会为了让 `maxp.numGlyphs` 与上游相同而补入不可达 glyph。
- 静态 TTF 不再为了 OTS 清除上游 `OVERLAP_SIMPLE` flags；这些 flags 会影响 FreeType rasterization，exact 样式应与上游保持一致。最终写出会强制重编译 `glyf`，并把带 `OVERLAP_SIMPLE` 的重复 flags 写成 OTS 可接受的 repeat 编码，而不是删除 bit 6。OTS 对上游 unhinted 和本派生 unhinted 可能仍打印 `maxp maxZones: 0`、`gasp` sentinel/丢表等基线警告，但返回码通过。
- VF 轮廓上游使用 Source Han Sans SC VF，因此部分字形与 Sarasa 上游静态 TTF 不会逐点完全一致；脚本会同步 Sarasa Ui 的位置/宽度规则，但不会伪造 VF 上游没有的逐点轮廓。静态 TTF 使用静态 Source Han Sans SC，因此 exact 样式会审计非数字/非冒号码位的 bbox、坐标、flags 和组件名一致性。

## 字重

VF 实例和静态 TTF 都使用更接近 Sarasa / CSS 的公开字重体系：

- `ExtraLight`：`200`
- `Light`：`300`
- `Normal`：`350`
- `Regular`：`400`
- `Medium`：`500`
- `Bold`：`700`
- `Heavy`：`900`

其中 `Normal`、`Medium`、`Heavy` 是本项目保留的扩展实例。`ExtraLight 200`
是公开字重口径；CJK 轮廓来源仍是 Source Han Sans SC 的 ExtraLight 口径
`250`；VF 通过 `avar`
轴映射让 public `wght=200` 对应 Source Han 内部 `wght=250`，同时让 Inter
对应 public `wght=200`；public `300` `350` `400` `500` `700` `900` 分别映射到
Source Han 和 Inter 的同数字上游位置。

## 文件

VF：

- [fonts/variable/Sarasa-Ui-VF-PropDigits-SC[wght].ttf](<fonts/variable/Sarasa-Ui-VF-PropDigits-SC[wght].ttf>)
- [fonts/variable/Sarasa-Ui-VF-PropDigits-SC-Italic[wght].ttf](<fonts/variable/Sarasa-Ui-VF-PropDigits-SC-Italic[wght].ttf>)

hinted 静态 TTF：

- [fonts/static/SarasaUiPropDigitsSC-TTF-1.0.39](fonts/static/SarasaUiPropDigitsSC-TTF-1.0.39)

unhinted 静态 TTF：

- [fonts/static/SarasaUiPropDigitsSC-TTF-Unhinted-1.0.39](fonts/static/SarasaUiPropDigitsSC-TTF-Unhinted-1.0.39)

每套静态版包含 14 个文件：7 个字重，每个字重有正体和 Italic。两套静态 TTF 都从 Sarasa 静态片段路径构建；hinted 版额外经过 `ttfautohint` 和 Sarasa 上游 Chlorophytum hint 流程，unhinted 版保留无 TrueType instructions 的静态输出。

## 构建

构建脚本是：

```powershell
python tools\build_sarasa_ui_propdigits_sc.py
```

如果只重建两套静态 TTF、保留现有 VF 输出，可以用：

```powershell
python tools\build_sarasa_ui_propdigits_sc.py --static-only
```

脚本会在缺失依赖或源文件时准备固定版本的构建输入：Sarasa Gothic `v1.0.39`、SarasaUiSC TTF `1.0.39` hinted/unhinted、Source Han Sans `2.005R` VF、Inter `v4.1`、Node.js `v24.16.0`，以及 Sarasa 上游 npm 依赖。Python 包依赖也会自动安装，包括 `fontTools`、`uharfbuzz`、`brotli`、`ttfautohint-py`、`py7zr` 和 AFDKO；静态 Source Han TTC 转换会使用 AFDKO 提供的 `otc2otf`/`otf2ttf`，脚本会同时查找系统 `PATH` 和当前 Python 的 Scripts 目录。已有的目标文件/目录会直接复用；默认工作目录为仓库同级目录。

因此，clone 后通常只需要直接运行构建脚本。脚本会把下载缓存放在同级 `source-archives/`，把 Sarasa Gothic 上游源码放在同级 `Sarasa-Gothic/`，把官方 SarasaUiSC 参考字体放在同级 `official-sarasa-ui-sc/`，把 VF 输入放在同级 `vf-sources/`，把固定 Node.js 运行时放在同级 `node/`。如果这些目录或文件已经存在，脚本会跳过下载/解包；如果没有，它会自动下载。默认会使用这份固定 Node.js，而不是系统里碰巧安装的 Node；只有显式设置 `SARASA_NODE`、`NODE` 或 `NPM` 时才会改用外部运行时。

可用环境变量覆盖：

- `SARASA_WORK_ROOT`
- `VF_SOURCE_DIR`
- `SOURCE_HAN_SC_VF`
- `INTER_VF`
- `INTER_ITALIC_VF`
- `REFERENCE_SARASA_ROOT`
- `REFERENCE_SARASA`
- `REFERENCE_SARASA_HINTED_DIR`
- `TTFAUTOHINT`
- `OTC2OTF`
- `OTF2TTF`
- `SARASA_SOURCE_DIR`
- `SARASA_NODE`
- `NODE`
- `SARASA_NODE_DIR`
- `NPM`
- `SARASA_CHLOROPHYTUM`
- `SARASA_HINT_JOBS`
- `SARASA_BUILD_CACHE`
- `SARASA_DISABLE_BUILD_CACHE`
- `SARASA_SKIP_SOURCE_BOOTSTRAP`

静态 hinted 构建会把 Chlorophytum 处理后的 FE 片段缓存到 `.build-cache/sarasa-propdigits-sc/`，并在正体/Italic 之间复用同一字重的 kanji/hangul 片段。缓存键包含输入字体、hcfg、Chlorophytum 包和启动脚本哈希；需要冷构建时可设置 `SARASA_DISABLE_BUILD_CACHE=1`，或用 `SARASA_BUILD_CACHE` 指向其他缓存目录。

字体检查报告见 [reports/font-inspection.json](reports/font-inspection.json)，构建报告见 [reports/Sarasa-Ui-VF-PropDigits-SC-report.json](reports/Sarasa-Ui-VF-PropDigits-SC-report.json)。

## 许可证

字体按 SIL Open Font License 1.1 分发，见 [LICENSE](LICENSE)。

这是修改版字体，不是 Sarasa Gothic、Source Han Sans 或 Inter 的官方发布。
