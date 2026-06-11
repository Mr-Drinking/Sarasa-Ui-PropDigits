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
- `Sarasa Ui ProDigits SC`

这些字体不是上游官方发布。请不要将其表述为 Sarasa Gothic、Source Han Sans 或 Inter 的官方版本。

构建说明：

- 可变字体系列直接合并 Source Han Sans SC VF 和 Inter VF，不从静态字重派生或插值。
- 静态 TTF 从修正后的 VF 实例化，并使用 `ttfautohint` 生成 hinted 版本。
- 与上游 Sarasa Ui 的有意差异是默认 ASCII 数字为变宽数字、字重级别采用 `250/300/350/400/500/700/900`，以及增加数字之间冒号上浮的 `calt` 规则。
- 为保持 TrueType glyph 数量限制，最终 VF/静态字体保留 Sarasa Ui 需要的本地化、Hangul Jamo、竖排和数字特性；Source Han 原始 `ccmp` 未保留。
