# Sarasa Ui VF PropDigits SC / Sarasa Ui ProDigits SC

这个仓库包含两个 Sarasa Ui SC 派生字体系列：

- **Sarasa Ui VF PropDigits SC**：正体和 Italic 可变字体，`wght` 轴为 `250..900`。
- **Sarasa Ui ProDigits SC**：从同一 VF 构建实例化出的 hinted 静态 TTF，包含 7 个字重及对应 Italic。

两个系列都把 ASCII 数字 `U+0030..U+0039` 设为默认变宽数字，并提供 OpenType `tnum`/`pnum` 在变宽数字和等宽数字之间切换。字体还包含一个 `calt` 规则：当冒号 `:` 位于两个数字之间时，自动替换为上浮冒号字形；普通冒号不受影响。

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

## 构建逻辑

VF 不从静态字重插值生成。它直接合并：

- `SourceHanSansSC-VF.ttf`
- `InterVariable.ttf`
- `InterVariable-Italic.woff2`

构建时对齐 Sarasa Ui 的处理方式：

- Inter 先烘焙 Sarasa 原版给 Inter 配置的 `ss03` 和 `cv10`。
- Inter 只接管 Sarasa 交给 Latin 源的码位；UI 标点、符号、CJK、Hangul 和 Jamo 保留 Source Han Sans SC VF。
- Source Han 侧烘焙 UI 标点需要的 `pwid` 替换，并执行 Sarasa 式符号清洗，例如 `·`、弯引号、短横、省略号、`⸺/⸻` 和注音扩展符号宽度处理。
- Hangul/Jamo 宽度归一到全角，修正上一版 VF 丢失 `U+1100..U+11FF` 和 Hangul 920 宽度的问题。
- 为保持 TrueType `65535` glyph 上限，最终 GSUB 保留 `locl`、Hangul Jamo、`vert/vrt2`、`tnum/pnum` 和数字冒号 `calt`；Source Han 原始 `ccmp` 未保留。

## 字重

VF 实例和静态 TTF 都使用思源黑体式字重级别：

- `ExtraLight` `250`
- `Light` `300`
- `Normal` `350`
- `Regular` `400`
- `Medium` `500`
- `Bold` `700`
- `Heavy` `900`

## 文件

VF：

- [fonts/variable/Sarasa-Ui-VF-PropDigits-SC[wght].ttf](<fonts/variable/Sarasa-Ui-VF-PropDigits-SC[wght].ttf>)
- [fonts/variable/Sarasa-Ui-VF-PropDigits-SC-Italic[wght].ttf](<fonts/variable/Sarasa-Ui-VF-PropDigits-SC-Italic[wght].ttf>)

静态 TTF：

- [fonts/static/SarasaUiProDigitsSC-TTF-1.0.39](fonts/static/SarasaUiProDigitsSC-TTF-1.0.39)

静态版包含 14 个文件：7 个字重，每个字重有正体和 Italic。静态 TTF 从修正后的 VF 实例化后使用 `ttfautohint` 处理。

## 构建

构建脚本是：

```powershell
python tools\build_sarasa_ui_sc_true_vf.py
```

默认源文件位置为仓库同级的 `vf-sources/`。可用环境变量覆盖：

- `VF_SOURCE_DIR`
- `SOURCE_HAN_SC_VF`
- `INTER_VF`
- `INTER_ITALIC_VF`
- `REFERENCE_SARASA`
- `TTFAUTOHINT`

## 校验

SHA-256 校验值见 [checksums/SHA256SUMS.txt](checksums/SHA256SUMS.txt)。

字体检查报告见 [reports/font-inspection.json](reports/font-inspection.json)，构建报告见 [reports/Sarasa-Ui-VF-PropDigits-SC-report.json](reports/Sarasa-Ui-VF-PropDigits-SC-report.json)。

## 许可证

字体按 SIL Open Font License 1.1 分发，见 [LICENSE](LICENSE)。

这是修改版字体，不是 Sarasa Gothic、Source Han Sans 或 Inter 的官方发布。
