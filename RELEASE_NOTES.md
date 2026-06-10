# 发布说明草稿

本次发布包含两个并列的 Sarasa Ui SC 派生字体系列，默认使用变宽 ASCII 数字。

包含字体：

- `Sarasa Ui VF PropDigits SC`：可变 TTF，包含正体和 Italic，`wght 250..900`。
- `Sarasa Ui ProDigits SC`：静态 TTF，包含 5 个字重及对应 Italic。

要点：

- ASCII 数字 `0..9` 默认使用变宽形式。
- 保留 `tnum`/`pnum` OpenType 特性，可在支持字体特性的应用中切换数字宽度。
- 可变字体使用 Source Han Sans SC VF 加 Inter Variable 构建。
- 可变字体的 `STAT` 和命名实例沿用 Source Han Sans SC VF 的字重级别名：`ExtraLight`、`Light`、`Normal`、`Regular`、`Medium`、`Bold`、`Heavy`。
- 静态字体来自 `SarasaUiProDigitsSC-TTF-1.0.39.zip` 的解包内容，并统一将字体名写作 `Sarasa Ui ProDigits SC`。

许可证：SIL Open Font License 1.1。
