Sarasa Ui PropDigits J TTF 1.0.39

本目录包含静态 TrueType 字体。这些字体从静态 SourceHanSans 和
Inter 源字体出发，经 Sarasa 的 pass1/kanji/hangul/pass2 构建路径生成，
然后补上 PropDigits 派生行为。

J 地区沿用 Sarasa 上游路径：CJK 底稿来自 SourceHanSans。

字重：

- ExtraLight 200
- Light 300
- Normal 350
- Regular 400
- Medium 500
- Bold 700
- Heavy 900

公开字重采用 Sarasa/CSS 口径：ExtraLight 是 200。CJK 轮廓来源仍是
Source Han Sans 的 ExtraLight 口径 250；VF 通过轴映射让 public
wght=200 对应 Source Han 内部 wght=250，而 Inter 对应 wght=200。

每个字重都包含正体和 Italic 文件。ASCII 数字默认使用比例宽度；
OpenType tnum 会恢复等宽数字，pnum 会把等宽数字切回比例数字。
静态 TTF 与 VF 使用一致的、与 Inter 兼容的 calt 冒号行为：
1:2 会上浮 ':'，1:a 和 a:2 不会上浮，1::2 等连续冒号上下文遵循
Inter 的 colon-run 规则。

name 表包含地区本地化显示名，例如：
更紗ゴシック Ui PropDigits J ExtraLight.
OS/2.achVendID 使用本派生项目的 MRDK，不继承上游 Sarasa Ui 的
???? 占位值。head.fontRevision 使用 OpenType fixed 数值 1.0392，
对应本仓库语义版本 1.0.39.2；nameID 5 写作 Version 1.0.39.2。
hinted 套件会对本项目实际生成的静态片段重新 hint：pass1 先经过
ttfautohint，随后 pass1/kanji/hangul 片段用 Sarasa 上游 Chlorophytum
hcfg 流程写入 TrueType instructions，最后由 pass2 合成最终 TTF。
Normal、Medium、Heavy 这类项目扩展字重也按当前轮廓重新生成 hint，
不会冒充官方 Sarasa 已发布静态字重。静态 PropDigits 会把 ':' remap
到已有的 pnum glyph，移除旧的冒号上下文替换，再追加与 Inter 一致的
colon-run calt 规则。
静态 TTF 保留静态 STAT 表，供现代应用识别 weight/italic 样式；这不会让
静态 TTF 变成可变字体。GSUB/GPOS 的 FeatureRecord 顺序、Script/LangSys
覆盖和基础 lookup 结构按对应样式的上游 Sarasa Ui J 静态字体套模板。
静态 TTF 最终会按对应 Sarasa Ui 参考字体裁剪 cmap，并同步非数字 metrics。
`palt` 下假名等已有 glyph 的定位值也按对应参考字体同步；连续长破折号（em dash）
在 calt/vert/vrt2 相关路径下按对应上游静态 Sarasa Ui 的替换行为校验。
对于 exact 静态样式，非数字/非冒号码位会保留上游 simple glyph flags、
glyf bbox 和组合字形组件名。静态 TTF 使用 post format 2，让默认比例数字
remap 到 U+0030..U+0039 后，相关 glyph names 仍能稳定保留。最终写出 glyf
时保留上游 OVERLAP_SIMPLE 语义，并用 OTS 可接受的 repeat 编码保存重复
overlap flags，而不是清除 bit 6。unhinted 套件中的 OTS maxZones/gasp 警告
继承自上游 unhinted 基线，返回码为 0。
glyph 总数不强行补齐到与上游一致；cmap 字形和布局可达的未编码字形会保留，
不可达 glyph 数量差异视为构建产物。
这些字体是修改派生版，不是 Sarasa Gothic、Source Han Sans 或 Inter 的官方发布。
