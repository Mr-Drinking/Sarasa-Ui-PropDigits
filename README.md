# Sarasa Ui VF PropDigits SC / Sarasa Ui ProDigits SC

这个仓库包含两个并列的 Sarasa Ui SC 派生字体系列：

- **Sarasa Ui VF PropDigits SC**：可变字体版本，包含正体和 Italic，`wght` 轴范围为 `250` 至 `900`。
- **Sarasa Ui ProDigits SC**：静态 TTF 版本，包含 5 个字重及对应 Italic。

两个系列都把 ASCII 数字 `U+0030..U+0039` 设为默认变宽数字，并保留 OpenType `tnum`/`pnum` 特性。支持字体特性的应用可以继续在变宽数字和等宽数字之间切换。

## 文件结构

```text
fonts/
  variable/
    Sarasa-Ui-VF-PropDigits-SC[wght].ttf
    Sarasa-Ui-VF-PropDigits-SC-Italic[wght].ttf
  static/
    SarasaUiProDigitsSC-TTF-1.0.39/
      SarasaUiProDigitsSC-*.ttf
      README.txt
      LICENSE-Sarasa-Gothic.txt
reports/
  Sarasa-Ui-VF-PropDigits-SC-report.json
  font-inspection.json
tools/
  build_sarasa_ui_sc_true_vf.py
checksums/
  SHA256SUMS.txt
```

## Sarasa Ui VF PropDigits SC

文件：

- [Sarasa-Ui-VF-PropDigits-SC[wght].ttf](<fonts/variable/Sarasa-Ui-VF-PropDigits-SC[wght].ttf>)
- [Sarasa-Ui-VF-PropDigits-SC-Italic[wght].ttf](<fonts/variable/Sarasa-Ui-VF-PropDigits-SC-Italic[wght].ttf>)

属性：

- 字体家族名：`Sarasa Ui VF PropDigits SC`
- 字重轴：`wght` 为 `250` 至 `900`，默认 `400`
- 字重级别名：`ExtraLight`、`Light`、`Normal`、`Regular`、`Medium`、`Bold`、`Heavy`
- 默认数字：变宽数字
- 数字特性：`tnum`、`pnum`
- 字形数：正体 `65482`，Italic `65446`

构建说明：

- CJK 部分来自 `SourceHanSansSC-VF.ttf`。
- 拉丁、西文和默认数字来自 Inter Variable。
- Source Han Sans SC VF 按 Sarasa 原版处理 glyph 限额的思路裁掉西文字形，由拉丁源补齐西文部分。
- Inter Variable 固定 `opsz 14`，并保留 `wght` 为 `250` 至 `900`。
- Inter 字形以带前缀的内部 glyph 名追加到 Source Han 子集后，最终字形数保持在 TrueType `65535` 限额内。
- Italic 版本使用 Inter Variable Italic；CJK 部分做 9.4 度倾斜处理。

## Sarasa Ui ProDigits SC

目录：

- [fonts/static/SarasaUiProDigitsSC-TTF-1.0.39](fonts/static/SarasaUiProDigitsSC-TTF-1.0.39)

包含文件：

- `SarasaUiProDigitsSC-ExtraLight.ttf`
- `SarasaUiProDigitsSC-ExtraLightItalic.ttf`
- `SarasaUiProDigitsSC-Light.ttf`
- `SarasaUiProDigitsSC-LightItalic.ttf`
- `SarasaUiProDigitsSC-Regular.ttf`
- `SarasaUiProDigitsSC-Italic.ttf`
- `SarasaUiProDigitsSC-SemiBold.ttf`
- `SarasaUiProDigitsSC-SemiBoldItalic.ttf`
- `SarasaUiProDigitsSC-Bold.ttf`
- `SarasaUiProDigitsSC-BoldItalic.ttf`

说明：

- 基于 Sarasa Gothic `1.0.39` 的 Sarasa Ui SC 静态 TTF。
- 字体家族名统一为 `Sarasa Ui ProDigits SC`。
- ASCII 数字默认映射到原字体已有的变宽数字字形。
- 原等宽数字字形和 `tnum` 特性保留。

## 安装

在 Windows 中选中需要安装的 `.ttf` 文件，右键选择“安装”或“为所有用户安装”。

如果应用支持可变字体，优先使用 `fonts/variable` 中的 VF；如果应用对可变字体支持不好，可以使用 `fonts/static` 中的静态 TTF。

## 校验

所有仓库文件的 SHA-256 校验值见：

- [checksums/SHA256SUMS.txt](checksums/SHA256SUMS.txt)

字体检查报告见：

- [reports/font-inspection.json](reports/font-inspection.json)

## 许可证

字体按 SIL Open Font License 1.1 分发，见 [LICENSE](LICENSE)。

这是修改版字体，不是 Sarasa Gothic、Source Han Sans 或 Inter 的官方发布。
