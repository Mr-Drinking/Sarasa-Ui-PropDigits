# 更纱黑体 Ui PropDigits

<p align="center">
  <img
    src="assets/Sarasa_Ui_VF_Specimen.svg"
    alt="Sarasa Ui VF"
    title="Sarasa Ui VF"
  >
</p>

## 为什么要制作它？

曾几何时，我试图使用[更纱黑体（Sarasa Gothic）](https://github.com/be5invis/sarasa-gothic)作为 Android 手机的默认字体（因为个人更喜欢 [SF Pro](https://developer.apple.com/cn/fonts/) / [Inter](https://rsms.me/inter/) 而非 [Roboto](https://fonts.google.com/specimen/Roboto)），但由于修改软件只支持一个字体，我需要一个可变字体。但可惜的是，[原版更纱并不支持](https://github.com/be5invis/Sarasa-Gothic/issues/314)，我只好自己动手，用 Codex 中的 GPT-5.5 制作了本字体的可变版本；静态版本以及比例宽数字则是源于我对其成为 [Unigram](https://github.com/unigramdev/unigram) / Windows 默认字体的需要。

## 简介

这个仓库包含 Sarasa Ui 的 PropDigits 多地区派生字体：

- **Sarasa Ui VF PropDigits CL / SC / TC / HC / J / K**：正体和 Italic 可变字体，公开 `wght` 轴为 `200..900`。
- **Sarasa Ui PropDigits CL / SC / TC / HC / J / K**：从静态 Source Han Sans、Inter 和必要的地区覆盖源按 Sarasa 静态片段路径构建的 TTF，包含 hinted 与 unhinted 两套，每套 7 个字重及对应 Italic。

CL 的传统旧字形覆盖跟随 [Shanggu Sans](https://github.com/GuiWonder/Shanggu) `1.028` 官方发布物：静态 TTF 使用 `ShangguSansTC-*.ttf`，VF 使用 `ShangguSansTC-VF.ttf`。这样静态和 VF 使用同一 Shanggu 发布口径，而不是继续绑定 Sarasa `1.0.39` 当时内置的旧 subset 转换产物。最终静态 CL 的公开 cmap、GSUB/GPOS feature 边界和非数字 metrics 仍按官方 SarasaUiCL 参考字体裁剪与同步；Shanggu 比官方 SarasaUiCL 多出的码位或旧字形 feature 不会额外暴露到本系列静态字体里。

两个系列都把 ASCII 数字 `U+0030..U+0039` 设为默认变宽数字，并提供 OpenType `tnum`/`pnum` 在变宽数字和等宽数字之间切换。VF 与静态 TTF 都按 Inter 的 `calt` 冒号行为处理数字冒号串：`1:2` 会上浮，`1:a`、`a:2`、`a:b` 不会上浮，`1::2`、`1:::a`、`a:::2` 等连续冒号上下文遵循 Inter 的 colon-run 规则。

## 地区

- `CL`：传统旧字形。静态 TTF 的汉字底稿先取 `SourceHanSansK`，再用 Shanggu Sans `1.028` 官方 `ShangguSansTC` 静态 TTF 覆盖传统旧字形；VF 使用 `SourceHanSansK-VF` 加 `ShangguSansTC-VF` 覆盖。最终公开字符集、layout feature 和非数字 metrics 以 SarasaUiCL 为边界。
- `SC`：简体中文，来源为 `SourceHanSansSC`。
- `TC`：繁体中文台湾字形，来源为 `SourceHanSansTC`。
- `HC`：繁体中文香港字形，来源为 `SourceHanSansHC`。
- `J`：日文字形，来源为 `SourceHanSans`。
- `K`：韩文字形，来源为 `SourceHanSansK`。

## 文件结构

```text
fonts/
  variable/
    Sarasa-Ui-VF-PropDigits-SC[wght].ttf
    Sarasa-Ui-VF-PropDigits-SC-Italic[wght].ttf
    Sarasa-Ui-VF-PropDigits-CL[wght].ttf
    Sarasa-Ui-VF-PropDigits-CL-Italic[wght].ttf
    Sarasa-Ui-VF-PropDigits-TC[wght].ttf
    Sarasa-Ui-VF-PropDigits-TC-Italic[wght].ttf
    ...
  static/
    SarasaUiPropDigitsCL-TTF-1.0.39/
    SarasaUiPropDigitsCL-TTF-Unhinted-1.0.39/
    SarasaUiPropDigitsSC-TTF-1.0.39/
    SarasaUiPropDigitsSC-TTF-Unhinted-1.0.39/
    ...
reports/
  Sarasa-Ui-PropDigits-report.json
  font-inspection.json
  release-audit.json
tools/
  build_sarasa_ui_propdigits_sc.py
```

## 构建逻辑

VF 不从静态字重插值生成。它直接合并对应地区的 CJK VF 与 Inter VF：

- `CL` 使用 `SourceHanSansK-VF.ttf`，并用 Shanggu Sans `1.028` 的 `ShangguSansTC-VF.ttf` 覆盖传统旧字形；静态 CL 使用同一 release 的 `ShangguSansTC-*.ttf` 作为传统旧字形覆盖源
- `SC` 使用 `SourceHanSansSC-VF.ttf`
- `TC` 使用 `SourceHanSansTC-VF.ttf`
- `HC` 使用 `SourceHanSansHC-VF.ttf`
- `J` 使用 `SourceHanSans-VF.ttf`
- `K` 使用 `SourceHanSansK-VF.ttf`
- Latin 正体使用 `InterVariable.ttf`，Italic 使用 `InterVariable-Italic.woff2` 或同名 TTF

构建时对齐 Sarasa Ui 的处理方式：

- VF 的公开 `wght` 轴是 `200..900`，默认值 `400`。Source Han Sans VF 内部仍按 `250..900` 裁剪并参与插值；最终 `avar` 把 public `200` 映射到 Source Han 内部 `250`，其余公开实例尽量映射到同数字上游位置。Inter VF 直接按 public `200..900` 裁剪。
- Inter 先烘焙 Sarasa 原版给 Inter 配置的 `ss03` 和 `cv10`。
- 码位归属遵循 Sarasa pass1 的优先级，并按 VF 源文件实际覆盖做兜底：Latin 和西文符号优先来自 Inter VF；CJK、Hangul、Jamo 和 Sarasa Ui 的本地化标点优先来自对应地区的 Source Han Sans VF。
- Source Han 侧烘焙 Ui 标点需要的 `pwid` 替换，并执行 Sarasa 式符号清洗，例如 `·`、弯引号、短横、省略号、`⸺/⸻` 和注音扩展符号宽度处理。
- Hangul / Jamo 宽度归一到全角。
- 最终 GSUB 保留上游 Sarasa Ui 有的 `ccmp`，并保留裁剪到上游覆盖范围的 `locl`、Hangul Jamo、`vert` `vrt2`、`tnum` `pnum`、连续长破折号（em dash）、上游暴露的空 `cv01..cv13` / `ss01..ss08` 标签，以及与 Inter 兼容的冒号 `calt`。Italic 按上游口径不暴露 `cv11`。
- 最终静态 `GSUB` 的 FeatureRecord 顺序和 Script / LangSys 覆盖顺序按对应地区、对应样式的上游 Sarasa Ui 静态字体套模板；`GPOS` 还会同步 FeatureRecord lookup index 与 LookupList 的类型、flag、subtable 形状，并把 `palt` 下假名等已有 glyph 的 SinglePos 取值同步到参考字体。GSUB lookup 内容保留本系列新增的 PropDigits 冒号上下文规则，同时会让连续长破折号的 `calt` 链接在 `hani`/`kana` 等脚本下与对应上游静态 Sarasa Ui 同样可达。
- VF 的 GPOS lookup 结构不以静态官方 Sarasa Ui 为逐项等同目标，因为 VF 由对应地区 CJK VF 与 Inter VF 合并生成；发布审计改为检查 VF 的压力实例、cmap、hmtx/vmtx、公开字重轴、数字/冒号行为，以及 exact-weight metrics。当前仅发现 `U+00B7` 在 CL/J/K 与 SC 的 side bearing 有地区标点边界差异，不属于广泛 Latin 源漂移。
- VF、hinted 静态 TTF 和 unhinted 静态 TTF 都包含 `STAT`。VF 的 `STAT` 描述 `wght`/`ital` 轴和命名实例；静态 TTF 的 `STAT` 只用于现代应用识别 weight / italic 样式，不表示静态文件仍有 `fvar` `gvar` 可变轴。
- `OS/2.achVendID` 使用本派生项目的 `MRDK`，不继承上游 Sarasa Ui 的 `????` 占位值，也不冒充 Source Han Sans 或 Inter 的官方 vendor。
- `head.fontRevision` 使用 OpenType 16.16 fixed 可表达的项目数值 `1.0392`，对应本仓库语义版本 `1.0.39.2`；`name` table 的 nameID 5 继续写作 `Version 1.0.39.2 ...`。
- 构建会按对应地区的上游 Sarasa Ui 同步非数字与非冒号 advance、横向 LSB、垂直指标、`GDEF`、`VORG`、`vmtx`、`head`/`OS/2` 中可安全继承的元数据字段；静态 exact 样式还会保留上游 simple glyph flags、glyf bbox 和组合字形组件名。数字、与 Inter 兼容的冒号上下文，以及 CL 跟随 Shanggu Sans `1.028` 官方 TTF/VF 而不是 Sarasa `1.0.39` 内置旧 subset 的轮廓来源，是本派生字体的刻意差异。
- 静态 TTF 不从 VF 实例化。hinted 和 unhinted 两套都使用静态 Source Han Sans 与静态 Inter，按 Sarasa 上游的 `pass1`、`kanji`、`hangul`、`pass2` 片段流程构建；CL 在 `kanji` 阶段额外使用 Shanggu Sans `1.028` 官方静态 TTF 覆盖传统旧字形。最终 TTF 会再按对应 Sarasa Ui 参考字体裁剪 cmap、回补空 `cv/ss` FeatureRecord、套用 GSUB Script / LangSys 模板和 GPOS 结构模板，并同步非数字 metrics；CL 的 `Normal`、`Medium`、`Heavy` 扩展字重分别用 Sarasa 静态路径里的 `Regular`、`SemiBold`、`Bold` 作为 reference 边界。随后默认数字和 `:` remap 到已有的 pnum glyph，清理旧冒号上下文替换后追加与 Inter shaping 样例一致的 colon-run `calt`，中文名、metadata、glyf flags / bbox、组件名和静态 `STAT` 也在最终 TTF 上同步。
- hinted 静态 TTF 会重新 hint 本项目实际生成的片段：`pass1` 先经过 `ttfautohint`，随后 `pass1`/`kanji`/`hangul` 片段用同版本 Chlorophytum `hcfg` 写入 TrueType instructions，最后由 `pass2` 合成。`Normal`/`Medium`/`Heavy` 这类项目扩展字重也按当前轮廓重新生成 hint，不冒充官方 Sarasa 已发布静态字重。
- unhinted 静态 TTF 使用相同的静态片段路径，但跳过 `ttfautohint` 和 Chlorophytum，直接由未 hint 的 `pass1`/`kanji`/`hangul` 合成；这是一套正式输出，供需要无 TrueType instructions 版本的使用场景选择。
- 静态 TTF 使用 `post` format 2 保存 glyph names；这是为了在默认比例数字改挂到 U+0030..U+0039 后，`glyph01332`/`glyph01334` 这类上游组件名仍能在保存、重开和审计中稳定保留。VF 仍保持原来的 `post`/GID 模型。
- glyph 总数不作为构建目标。脚本会保留和同步 cmap 字形以及 GSUB/GPOS/GDEF 可达的未编码 glyph；不会为了让 `maxp.numGlyphs` 与上游相同而补入不可达 glyph。
- 静态 TTF 不再为了 OTS 清除上游 `OVERLAP_SIMPLE` flags；这些 flags 会影响 FreeType rasterization，exact 样式应与上游保持一致。最终写出会强制重编译 `glyf`，并把带 `OVERLAP_SIMPLE` 的重复 flags 写成 OTS 可接受的 repeat 编码，而不是删除 bit 6。OTS 对上游 unhinted 和本派生 unhinted 可能仍打印 `maxp maxZones: 0`、`gasp` sentinel/丢表等基线警告，但返回码通过。

## 字重

VF 实例和静态 TTF 都使用更接近 Sarasa / CSS 的公开字重体系：

- `ExtraLight`：`200`
- `Light`：`300`
- `Normal`：`350`
- `Regular`：`400`
- `Medium`：`500`
- `Bold`：`700`
- `Heavy`：`900`

其中 `Normal`、`Medium`、`Heavy` 是本项目保留的扩展实例。`ExtraLight 200` 是公开字重口径；CJK 轮廓来源仍是 Source Han Sans 的 ExtraLight 口径 `250`，不会伪造 Source Han Sans 不存在的 CJK `200` 轮廓。VF 通过 `avar` 轴映射让 public `wght=200` 对应 Source Han 内部 `wght=250`，同时让 Inter 对应 public `wght=200`；public `300` `350` `400` `500` `700` `900` 分别映射到 Source Han 和 Inter 的同数字上游位置。

## 文件

- VF：[fonts/variable](fonts/variable)
- hinted 静态 TTF：[fonts/static](fonts/static)
- unhinted 静态 TTF：[fonts/static](fonts/static)

每个地区、每套静态版包含 14 个文件：7 个字重，每个字重有正体和 Italic。两套静态 TTF 都从 Sarasa 静态片段路径构建；hinted 版额外经过 `ttfautohint` 和 Sarasa 上游 Chlorophytum 的 hint 流程，unhinted 版保留无 TrueType instructions 的静态输出。

发布页会提供按地区拆分和全集打包的 TTF 压缩包：

每个地区有三套包，共 18 个 ZIP：

- `Sarasa-Ui-VF-PropDigits-{REGION}-TTF-1.0.39.2.zip`：单个地区的正体和 Italic 可变 TTF。
- `SarasaUiPropDigits{REGION}-TTF-1.0.39.2.zip`：单个地区的 hinted 静态 TTF。
- `SarasaUiPropDigits{REGION}-TTF-Unhinted-1.0.39.2.zip`：单个地区的 unhinted 静态 TTF。

另有三套全地区合集：

- `Sarasa-Ui-VF-PropDigits-TTF-1.0.39.2.zip`：全部地区的可变 TTF。
- `SarasaUiPropDigits-TTF-1.0.39.2.zip`：全部地区的 hinted 静态 TTF。
- `SarasaUiPropDigits-TTF-Unhinted-1.0.39.2.zip`：全部地区的 unhinted 静态 TTF。

合计 21 个 ZIP。版本号中的 `1.0.39` 表示上游 Sarasa Gothic 基线，最后的 `.2` 是本仓库自己的衍生发布版本号。

## 构建

构建脚本是：

```powershell
python tools\build_sarasa_ui_propdigits_sc.py
```

如果只重建两套静态 TTF、保留现有 VF 输出，可以用：

```powershell
python tools\build_sarasa_ui_propdigits_sc.py --static-only
```

如果只构建指定地区，可以用：

```powershell
python tools\build_sarasa_ui_propdigits_sc.py --regions SC,TC
```

静态 hinted 构建默认使用本机 CPU 核心数作为 Chlorophytum 并行数。需要限制或指定并行数时：

```powershell
$env:SARASA_HINT_JOBS = "16"
python tools\build_sarasa_ui_propdigits_sc.py
```

中断后需要继续静态构建时，可以显式使用 `--resume-static`：

```powershell
python tools\build_sarasa_ui_propdigits_sc.py --static-only --regions SC --resume-static
```

恢复模式不会清空现有静态输出目录，只会跳过已经完整存在的字重；同一字重必须同时存在 hinted/unhinted、正体/Italic 四个文件才会被视为完整。默认构建仍会先清理目标地区的静态 TTF 输出，以保证从零复现。

脚本会在缺失依赖或源文件时准备固定版本的构建输入：Sarasa Gothic `v1.0.39`、各地区 `SarasaUi{REGION}` TTF `1.0.39` hinted/unhinted、Source Han Sans `2.005R` VF、Shanggu Sans `1.028` 静态 TTF 与 VF、Inter `v4.1`、Node.js `v26.3.0`，以及 Sarasa 上游 npm 依赖。Python 包依赖也会自动安装并固定到本次验证的版本：`fontTools 4.63.0`、`uharfbuzz 0.55.0`、`brotli 1.2.0`、`ttfautohint-py 0.6.0`、`py7zr 1.1.0` 和 `afdko 5.0.1`。静态 Source Han TTC 转换会使用 AFDKO 提供的 `otc2otf`/`otf2ttf`；CL 的 Shanggu 覆盖源直接使用 Shanggu 官方发布 TTF，不再通过本地 AFDKO 把 Sarasa 内置旧 subset OTF 转成 TTF。已有的目标文件/目录会直接复用；默认工作目录为仓库同级目录。

因此，clone 后通常只需要直接运行构建脚本。脚本会把下载缓存放在同级 `source-archives/`，把 Sarasa Gothic 上游源码放在同级 `Sarasa-Gothic/`，把官方 Sarasa Ui 参考字体放在同级 `official-sarasa-ui/`，把 VF 和 Shanggu 静态覆盖输入放在同级 `vf-sources/`，其中 CL 覆盖用的 `ShangguSansTC-VF.ttf` 会放在 `vf-sources/shanggu-1.028/`，`ShangguSansTC-*.ttf` 会放在 `vf-sources/shanggu-1.028/static/`；固定 Node.js 运行时放在同级 `node/`。如果这些目录或文件已经存在，脚本会跳过下载/解包；如果没有，它会自动下载。默认会使用这份固定 Node.js，而不是系统里碰巧安装的 Node；只有显式设置 `SARASA_NODE`、`NODE` 或 `NPM` 时才会改用外部运行时。

这些固定版本是本次发布的可复现输入，不是永久锁死的上游边界。未来升级 Sarasa Gothic、Shanggu Sans、Inter 或 Source Han Sans 时，应同步更新脚本中的版本常量、下载校验和与审计基准，并重新生成字体与报告。

可用环境变量覆盖：

- `SARASA_WORK_ROOT`
- `VF_SOURCE_DIR`
- `SOURCE_HAN_SC_VF`
- `SOURCE_HAN_TC_VF`
- `SOURCE_HAN_HC_VF`
- `SOURCE_HAN_J_VF`
- `SOURCE_HAN_K_VF`
- `SHANGGU_CL_VF`
- `CLASSICAL_CL_VF`
- `SHANGGU_SANS_TC_VF`
- `SHANGGU_CLASSICAL_VF`
- `SHANGGU_CL_STATIC_DIR`
- `CLASSICAL_CL_STATIC_DIR`
- `SHANGGU_SANS_TC_STATIC_DIR`
- `SHANGGU_CLASSICAL_STATIC_DIR`
- `INTER_VF`
- `INTER_ITALIC_VF`
- `REFERENCE_SARASA_ROOT`
- `REFERENCE_SARASA_SC_ROOT`
- `REFERENCE_SARASA`
- `REFERENCE_SARASA_HINTED_DIR`
- `TTFAUTOHINT`
- `OTC2OTF`
- `OTF2TTF`
- `SARASA_SOURCE_DIR`
- `SARASA_NODE`
- `SARASA_NODE_DIR`
- `NODE`
- `NPM`
- `SARASA_CHLOROPHYTUM`
- `SARASA_HINT_JOBS`
- `SARASA_BUILD_CACHE`
- `SARASA_DISABLE_BUILD_CACHE`
- `SARASA_SKIP_SOURCE_BOOTSTRAP`

静态 hinted 构建会把 Chlorophytum 处理后的 FE 片段缓存到 `.build-cache/sarasa-ui-propdigits/`，并在正体/Italic 之间复用同一地区、同一字重的 kanji/hangul 片段。缓存键包含输入字体、hcfg、Chlorophytum 包和启动脚本哈希；需要冷构建时可设置 `SARASA_DISABLE_BUILD_CACHE=1`，或用 `SARASA_BUILD_CACHE` 指向其他缓存目录。

字体检查报告见 [reports/font-inspection.json](reports/font-inspection.json)，构建报告见 [reports/Sarasa-Ui-PropDigits-report.json](reports/Sarasa-Ui-PropDigits-report.json)，发布前 exact 与 layout/shaping 审计见 [reports/release-audit.json](reports/release-audit.json)。layout 模板审计会逐个静态 TTF 比对 GSUB FeatureRecord 顺序、空 `cv/ss` 标签、Script / LangSys feature 顺序，以及 GPOS FeatureRecord/LookupList 结构；shaping 审计会覆盖静态 exact `palt` 下 `かなカナ` advance、静态连续长破折号在 `calt`/`vert`/`vrt2` 下的替换路径，以及 `head.fontRevision` 与 nameID 5 版本一致性。

## 许可证

字体按 SIL Open Font License 1.1 分发，见 [LICENSE](LICENSE)。

这是修改版字体，不是 Sarasa Gothic、Source Han Sans、Inter 或 Shanggu Sans 的官方发布。
