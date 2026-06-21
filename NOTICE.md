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
- hinted 静态 TTF 会对本项目实际生成的片段重新 hint：`pass1` 先经过 `ttfautohint`，随后 `pass1`/`kanji`/`hangul` 片段用 Sarasa 上游 Chlorophytum `hcfg` 写入 TrueType instructions，最后由 `pass2` 合成。unhinted 静态 TTF 是正式的无 TrueType instructions 输出。
- VF、hinted 静态 TTF 和 unhinted 静态 TTF 都保留 `STAT`。静态 `STAT` 只描述单个静态实例的 weight/italic 样式，不表示静态文件仍有可变轴。
- `OS/2.achVendID` 使用 `MRDK` 标识本仓库修改版，不沿用上游 Sarasa Ui 的 `????` 占位值，也不表示这些字体是 Source Han Sans 或 Inter 的官方版本。
- 与上游 Sarasa Ui 的有意差异是默认 ASCII 数字为变宽数字、公开字重级别采用 Sarasa/CSS 口径 `200/300/350/400/500/700/900`，VF 与静态 TTF 都使用与 Inter 一致的上下文冒号 colon-run `calt`，以及 CL 跟随 Shanggu Sans `1.028` 官方 TTF/VF 发布物而不是 Sarasa `1.0.39` 内置旧 subset 转换产物。其中 public ExtraLight `200` 的 CJK 来源仍是 Source Han Sans ExtraLight `250`，不会伪造 Source Han Sans 不存在的 CJK `200` 轮廓；Shanggu 比官方 SarasaUiCL 多出的码位或旧字形 feature 不会额外暴露到静态 CL 公开字体里。
- 最终字体保留 `ccmp`、裁剪到上游 Sarasa Ui 覆盖范围的 `locl`、Hangul Jamo、`vert/vrt2`、数字特性，以及上游 Sarasa Ui 暴露的空 `cv/ss` FeatureRecord。静态 TTF 的 GSUB Script / LangSys 顺序和 GPOS FeatureRecord/LookupList 结构按对应地区参考字体同步；GSUB lookup 内容保留本派生项目新增的 PropDigits 冒号上下文规则。静态 TTF 最终写出时保留上游 `OVERLAP_SIMPLE` 语义，并用 OTS 可接受的 repeat 编码保存重复 overlap flags；unhinted 的 OTS `maxZones/gasp` 警告继承自上游基线。
