# 声明

本仓库包含基于 Sarasa Gothic、Source Han Sans 和 Inter 修改得到的字体文件。

版权声明：

- Copyright (c) 2015-2025, Renzhi Li (aka. Belleve Invis, belleve@typeof.net)。
- Portions Copyright (c) 2016 The Inter Project Authors。
- Portions Copyright (c) 2014-2021 Adobe Systems Incorporated，Reserved Font Name 为 `Source`。
- Portions Copyright (c) 2012 Google Inc.。

字体按 SIL Open Font License 1.1 分发，见 [LICENSE](LICENSE)。

本仓库中的修改版字体家族：

- `Sarasa Ui VF PropDigits SC`
- `Sarasa Ui PropDigits SC`

这些字体不是上游官方发布。请不要将其表述为 Sarasa Gothic、Source Han Sans 或 Inter 的官方版本。

构建说明：

- 可变字体系列直接合并 Source Han Sans SC VF 和 Inter VF，不从静态字重派生或插值。
- 静态 TTF 不从 VF 实例化；hinted 与 unhinted 两套都使用静态 Source Han Sans SC 和静态 Inter，按 Sarasa 上游的 `pass1`、`kanji`、`hangul`、`pass2` 片段流程构建。
- hinted 静态 TTF 使用 Sarasa 上游的 `ttfautohint` + Chlorophytum `hcfg` 顺序；对于官方存在且轮廓相同的 exact 样式，最终 TTF 会同步官方 TrueType instruction tables 和同名 glyph program。unhinted 静态 TTF 是正式的无 TrueType instructions 输出。
- VF、hinted 静态 TTF 和 unhinted 静态 TTF 都保留 `STAT`。静态 `STAT` 只描述单个静态实例的 weight/italic 样式，不表示静态文件仍有可变轴。
- 与上游 Sarasa Ui 的有意差异是默认 ASCII 数字为变宽数字、字重级别采用 `250/300/350/400/500/700/900`，以及冒号上浮规则：VF 使用严格的数字间冒号 `calt`；静态 TTF 复用 Inter/Sarasa 保留的较宽上下文冒号 `calt`。
- 最终字体保留 `ccmp`、裁剪到上游 Sarasa Ui 覆盖范围的 `locl`、Hangul Jamo、`vert/vrt2` 和数字特性，并在静态 TTF 最终写出时清理 simple glyph overlap flag 以通过 OTS/web-font sanitizer。
