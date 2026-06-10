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

可变字体系列保留 Source Han Sans SC VF 的 CJK 部分，并使用 Inter Variable 提供拉丁、西文和默认数字。为避免触及 TrueType 字形数上限，可变字体构建时沿用 Sarasa 原版处理 glyph 限额的思路：Source Han 子集裁掉西文字形，由拉丁源提供西文部分。

静态字体系列来自 `SarasaUiProDigitsSC-TTF-1.0.39.zip` 的解包内容；原许可证文件保留在 `fonts/static/SarasaUiProDigitsSC-TTF-1.0.39/` 中。
