# Sarasa Ui (VF) PropDigits SC

这个仓库包含两个 Sarasa Ui SC 派生字体系列：

- **Sarasa Ui VF PropDigits SC**：正体和 Italic 可变字体，`wght` 轴为 `250..900`。
- **Sarasa Ui PropDigits SC**：从同一 VF 构建实例化出的 hinted 静态 TTF，包含 7 个字重及对应 Italic。

两个系列都把 ASCII 数字 `U+0030..U+0039` 设为默认变宽数字，并提供 OpenType `tnum`/`pnum` 在变宽数字和等宽数字之间切换。字体还包含一个 `calt` 规则：当冒号 `:` 位于两个数字之间时，自动替换为上浮冒号字形；普通冒号不受影响。

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
reports/
  Sarasa-Ui-VF-PropDigits-SC-report.json
  font-inspection.json
tools/
  build_sarasa_ui_sc_true_vf.py
```

## 构建逻辑

VF 不从静态字重插值生成。它直接合并：

- `SourceHanSansSC-VF.ttf`
- `InterVariable.ttf`
- `InterVariable-Italic.woff2`

构建时对齐 Sarasa Ui 的处理方式：

- Inter 先烘焙 Sarasa 原版给 Inter 配置的 `ss03` 和 `cv10`。
- 码位归属遵循 Sarasa pass1 的优先级，并按 VF 源文件实际覆盖做兜底：Latin 和西文符号优先来自 Inter VF；CJK、Hangul、Jamo 和 Sarasa Ui 的本地化标点优先来自 Source Han Sans SC VF。
- Source Han 侧烘焙 Ui 标点需要的 `pwid` 替换，并执行 Sarasa 式符号清洗，例如 `·`、弯引号、短横、省略号、`⸺/⸻` 和注音扩展符号宽度处理。
- Hangul/Jamo 宽度归一到全角。
- 最终 GSUB 保留上游 Sarasa Ui 有的 `ccmp`，并保留裁剪到上游覆盖范围的 `locl`、Hangul Jamo、`vert/vrt2`、`tnum/pnum`、连续 em dash 和数字冒号 `calt`。
- 最终 `GSUB`/`GPOS` 的 FeatureRecord 顺序、Script/LangSys 覆盖和 lookup 数量按对应样式的上游 Sarasa Ui SC 静态字体套模板，避免只有 default langsys 而缺少 `JAN`/`KOR`/`ZHH`/`ZHS`/`ZHT`、Latin `CAT`/`MOL`/`ROM` 等语言系统。
- VF 和静态 TTF 都包含 `STAT`。VF 的 `STAT` 描述 `wght`/`ital` 轴和命名实例；静态 TTF 的 `STAT` 只用于现代应用识别 weight/italic 样式，不表示静态文件仍有 `fvar/gvar` 可变轴。
- `name` 表包含简体中文显示名：静态为 `更纱黑体 Ui PropDigits SC`，VF 为 `更纱黑体 Ui VF PropDigits SC`。
- 构建会按上游 Sarasa Ui SC 同步非数字/非冒号 advance、横向 LSB、垂直指标、`GDEF`、`VORG`、`vmtx`、`head`/`OS/2` 中可安全继承的元数据字段；数字和位于数字之间的冒号是本派生字体的刻意差异。
- glyph 总数不作为构建目标。脚本会保留和同步 cmap 字形以及 GSUB/GPOS/GDEF 可达的未编码 glyph；不会为了让 `maxp.numGlyphs` 与上游相同而补入不可达 glyph。
- 轮廓上游使用 Source Han Sans SC VF，因此部分字形与 Sarasa 上游静态 TTF 不会逐点完全一致；脚本会同步 Sarasa Ui 的位置/宽度规则，但不会伪造 VF 上游没有的逐点轮廓。

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

- [fonts/static/SarasaUiPropDigitsSC-TTF-1.0.39](fonts/static/SarasaUiPropDigitsSC-TTF-1.0.39)

静态版包含 14 个文件：7 个字重，每个字重有正体和 Italic。静态 TTF 从当前 VF 构建实例化后使用 `ttfautohint` 处理，并在 hinting 后重建静态专用 `STAT`。

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

字体检查报告见 [reports/font-inspection.json](reports/font-inspection.json)，构建报告见 [reports/Sarasa-Ui-VF-PropDigits-SC-report.json](reports/Sarasa-Ui-VF-PropDigits-SC-report.json)。

## 许可证

字体按 SIL Open Font License 1.1 分发，见 [LICENSE](LICENSE)。

这是修改版字体，不是 Sarasa Gothic、Source Han Sans 或 Inter 的官方发布。
