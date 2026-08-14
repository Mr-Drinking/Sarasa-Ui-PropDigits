# 声明

本仓库包含基于 Sarasa Gothic、Source Han Sans 和 Inter 修改得到的字体文件。

版权声明：

- Copyright (c) 2015-2025, Renzhi Li (aka. Belleve Invis, belleve@typeof.net)。
- Portions Copyright (c) 2016 The Inter Project Authors。
- Portions Copyright (c) 2014-2021 Adobe Systems Incorporated，Reserved Font Name 为 `Source`。
- Portions Copyright (c) 2012 Google Inc.。
- CL 的传统旧字形覆盖来自 Shanggu Sans `1.028` 的官方静态 TTF 和 `ShangguSansTC-VF.ttf`。Shanggu Fonts 项目按 SIL Open Font License 1.1 分发。

字体按 SIL Open Font License 1.1 分发，见 [LICENSE](LICENSE)。

本仓库中的修改版字体家族：

- `Sarasa Ui VF PropDigits SC`
- `Sarasa Ui VF PropDigits CL`
- `Sarasa Ui VF PropDigits TC`
- `Sarasa Ui VF PropDigits HC`
- `Sarasa Ui VF PropDigits J`
- `Sarasa Ui VF PropDigits K`
- `Sarasa Ui PropDigits CL`
- `Sarasa Ui PropDigits SC`
- `Sarasa Ui PropDigits TC`
- `Sarasa Ui PropDigits HC`
- `Sarasa Ui PropDigits J`
- `Sarasa Ui PropDigits K`

这些字体不是上游官方发布。请不要将其表述为 Sarasa Gothic、Source Han Sans 或 Inter 的官方版本。

构建说明：

- 可变字体系列直接合并对应地区的 CJK VF 和 Inter VF，不从静态字重派生或插值。SC/TC/HC/J/K 使用对应的 Source Han Sans VF；CL 以 `SourceHanSansK-VF` 为底稿，并使用 Shanggu Sans `1.028` 的 `ShangguSansTC-VF` 覆盖传统旧字形。
- 静态 TTF 不从 VF 实例化；hinted 与 unhinted 两套都使用对应地区的静态 Source Han Sans 和静态 Inter，按 Sarasa 上游的 `pass1`、`kanji`、`hangul`、`pass2` 片段流程构建。CL 在 `kanji` 阶段使用 Shanggu Sans `1.028` 官方 `ShangguSansTC-*.ttf` 覆盖传统旧字形，以保持 CL 静态和 VF 都跟随同一 Shanggu 发布口径；最终静态 CL 的公开 cmap、GSUB/GPOS feature 边界和非数字 metrics 仍按官方 SarasaUiCL 参考字体裁剪与同步。
- hinted 静态 TTF 会对本项目实际生成的片段重新 hint。每个字重固定使用与 Sarasa 上游相同顺序的完整分析环境：8 个家族、6 个地区、正斜体共 96 个 `pass1`，另有 6 个 `kanji` 和 6 个 `hangul`，全部高层 hint 最后按上游 `GroupInstr` 顺序交给一次统一 `instruct`。高层 hint store 的 glyph 项按数字 GID 规范化，`sharedHints` 则保留 Sarasa hcfg 的 `Ideograph`、`Hiragana`、`Katakana` 语义顺序；其位置决定 TrueType 函数 ID，因此不能按名称排序。`Normal`、`Medium`、`Heavy` 的 Ui 与 FE 使用本项目实际 350、500、900 轮廓；没有同名上游成品的辅助拉丁环境和 hcfg 分别取 `Regular`、`SemiBold`、`Bold` 边界，不复制相邻成品的 glyph instructions。缓存以完整字重分组为单位，只保存高层 hint 数据，并把该顺序规则纳入缓存版本；指定地区构建和断点恢复也不会缩小分析环境。unhinted 静态 TTF 是正式的无 TrueType instructions 输出。
- VF、hinted 静态 TTF 和 unhinted 静态 TTF 都保留 `STAT`。静态 `STAT` 只描述单个静态实例的 weight/italic 样式，不表示静态文件仍有可变轴。
- `OS/2.achVendID` 使用 `MRDK` 标识本仓库修改版，不沿用上游 Sarasa Ui 的 `????` 占位值，也不表示这些字体是 Source Han Sans 或 Inter 的官方版本。
- `head.fontRevision` 使用 OpenType 16.16 fixed 数值 `1.0402` 对应本仓库版本 `1.0.40.2`；nameID 5 以 `Version 1.0402; project 1.0.40.2; ...` 开头，使首个 OpenType `major.minor` 数值与 `head` 一致，同时保留完整发布版本。版本号中的 `1.0.40` 是 Sarasa Gothic 上游基线，最后的 `.2` 是本仓库衍生版本号。
- 与上游 Sarasa Ui 的有意差异是默认 ASCII 数字为变宽数字、公开字重级别采用 Sarasa/CSS 口径 `200/300/350/400/500/700/900`，VF 与静态 TTF 都使用与 Inter 一致的上下文冒号 colon-run `calt`，以及 CL 跟随 Shanggu Sans `1.028` 官方 TTF/VF 发布物而不是 Sarasa `1.0.40` 内置旧 subset 转换产物。其中 public ExtraLight `200` 的 CJK 来源仍是 Source Han Sans ExtraLight `250`，不会伪造 Source Han Sans 不存在的 CJK `200` 轮廓；Shanggu 比官方 SarasaUiCL 多出的码位或旧字形 feature 不会额外暴露到静态 CL 公开字体里。
- 最终字体保留 `ccmp`、裁剪到上游 Sarasa Ui 覆盖范围的 `locl`、Hangul Jamo、`vert/vrt2`、数字特性，以及上游 Sarasa Ui 暴露的空 `cv/ss` FeatureRecord。静态 TTF 的 GSUB Script / LangSys 顺序和基础 GPOS FeatureRecord/LookupList 结构按对应地区参考字体同步，`palt` 下假名等已有 glyph 的定位值和连续长破折号（em dash）的替换路径也按对应上游静态 Sarasa Ui 校验；GSUB lookup 内容保留本派生项目新增的 PropDigits 冒号上下文规则。随后按照 Noto CJK 官方交付流程追加 GPOS `chws/vchw`，分别实现横排和竖排连续全角标点的上下文压缩。该特性固定使用 `chws_tool 1.4.5` 与 `east-asian-spacing 1.4.5`，只新增 GPOS FeatureRecord/contextual positioning lookup，不修改 cmap、轮廓、hint、advance、GSUB、GDEF 或公开字重轴。VF 的基础 GPOS lookup 结构可能因 VF 合并路径与静态官方字体不同，但发布审计会检查 VF 压力实例、cmap、hmtx/vmtx、数字/冒号、`chws/vchw` 和 exact-weight metrics。静态 TTF 最终写出时保留上游 `OVERLAP_SIMPLE` 语义，并用 OTS 可接受的 repeat 编码保存重复 overlap flags；unhinted 的 OTS `maxZones/gasp` 警告继承自上游基线。
- `chws/vchw` 是来自 Noto CJK 交付构建的特性。Source Han Sans `2.005R` 底稿及 Sarasa Gothic `1.0.40` 参考字体本身不含这两个 FeatureRecord；Noto CJK 与 Source Han Sans 共用 CJK 字形源，但在发布阶段另行执行 `add-chws` 后处理。本项目采用最新版工具实现同一特性，不声称它由 Adobe 的 Source Han Sans 二进制原生提供。
- 相对 `v1.0.40`，构建环境升级到 Node.js `26.7.0`、`uharfbuzz 0.56.0`、`ttfautohint-py 0.6.1` 和 `py7zr 1.1.3`。Node 仍执行 Sarasa `1.0.40` 的固定 lockfile；`ttfautohint-py 0.6.1` 未更换 hint 算法；`py7zr` 只负责解包；HarfBuzz 升级后的 `chws/vchw` GPOS 已与旧核心结果对照一致。
- 发布前使用 OTS `9.2.0` 复核全部 180 个字体，返回码全部通过；84 个 unhinted 静态 TTF 的 `maxZones/gasp` 信息与官方 unhinted Sarasa Ui 基线相同。FontBakery `1.1.0` 的 `opentype/font_version` 检查对全部项目字体通过；代表性 GDEF mark、spacing mark 和 `xAvgCharWidth` WARN 也存在于对应官方 Sarasa Ui 样本。
