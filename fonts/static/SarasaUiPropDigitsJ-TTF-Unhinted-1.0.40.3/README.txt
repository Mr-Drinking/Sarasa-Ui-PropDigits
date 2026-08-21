Sarasa Ui PropDigits J TTF Unhinted 1.0.40.3

本目录包含静态 TrueType 字体。这些字体从静态 SourceHanSans 和
Inter 源字体出发，经 Sarasa 的 pass1/kanji/hangul/pass2 构建路径生成，
然后补上 PropDigits 派生行为。

J 地区沿用 Sarasa 上游路径：CJK 底稿来自 SourceHanSans。

字重：

- ExtraLight 200
- Light 300
- Regular 400
- SemiBold 600
- Bold 700
- Heavy 900

ExtraLight、Light、Regular、SemiBold、Bold 与上游 Sarasa 的公开静态样式
一致；Heavy 900 是本项目保留的扩展实例。SemiBold 600 沿用 Sarasa 的静态
配对：CJK 使用 Source Han Sans Medium 500，Latin 使用 Inter SemiBold 600。

公开字重采用 Sarasa/CSS 口径：ExtraLight 是 200。CJK 轮廓来源仍是
Source Han Sans 的 ExtraLight 口径 250；VF 通过轴映射让 public
wght=200 对应 Source Han 内部 wght=250，并让 public wght=600 对应
Source Han 内部 wght=500；Inter 在两个位置分别对应 200 和 600。

每个字重都包含正体和 Italic 文件。ASCII 数字默认使用比例宽度；
OpenType tnum 会恢复等宽数字，pnum 会把等宽数字切回比例数字。
静态 TTF 与 VF 使用一致的、与 Inter 兼容的 calt 冒号行为：
1:2 会上浮 ':'，1:a 和 a:2 不会上浮，1::2 等连续冒号上下文遵循
Inter 的 colon-run 规则。

单个 U+2014 保留原比例宽。连续两个 U+2014 由 calt 把第二字替换为水平
延续字形，并以补数 advance 让横排总宽严格等于 2em。竖排时 vert/vrt2
把两字替换为同一个 uniFE31，再由 GPOS PairPos 按实际轮廓端面和斜率设置
第二字的 XPlacement/YPlacement 而不改变 YAdvance；正体只上移、Italic
同时横移并上移，使竖排总 advance 也严格等于 2em。两半使用相同轮廓和
hint 分类，calt 与竖排替换的不同 lookup 执行顺序都必须得到相同结果。

最终成品还会按 Noto CJK 的官方交付流程加入 GPOS chws/vchw：chws
用于横排连续全角标点的上下文压缩，vchw 用于对应的竖排压缩。实现固定使用
chws_tool 1.4.5 与 east-asian-spacing 1.4.5；Source Han Sans 2.005R
底稿本身不含这两个 FeatureRecord，因此它们在所有轮廓、hint、metrics 和
Sarasa layout 模板处理完成后统一追加。

name 表包含地区本地化显示名，例如：
更紗ゴシック Ui PropDigits J ExtraLight.
OS/2.achVendID 使用本派生项目的 MRDK，不继承上游 Sarasa Ui 的
???? 占位值。head.fontRevision 使用 OpenType fixed 数值 1.0403，
对应本仓库版本 1.0.40.3；nameID 5 以 OpenType 数值 Version 1.0403
开头，并在后续 project 字段保留完整版本 1.0.40.3。
unhinted 套件同样沿用上游 Sarasa 的静态片段构建路径，但直接用
未 hint 的 pass1/kanji/hangul 片段进入 pass2。它会跳过
ttfautohint 和 Chlorophytum，提供正式的无 TrueType instructions
静态输出。
静态 TTF 保留静态 STAT 表，供现代应用识别 weight/italic 样式；这不会让
静态 TTF 变成可变字体。GSUB/GPOS 的 FeatureRecord 顺序、Script/LangSys
覆盖和基础 lookup 结构按对应样式的上游 Sarasa Ui J 静态字体套模板；
随后追加 Noto CJK chws/vchw 的 FeatureRecord 和 contextual positioning lookup。
静态 TTF 最终会按对应 Sarasa Ui 参考字体裁剪 cmap，并同步非数字 metrics。
`palt` 下假名等已有 glyph 的定位值按展开轮廓结构与横竖 metrics 建立语义
映射后从参考字体同步，不依赖 post format 3 产生的跨字体不稳定自动名。连续 U+2014
保留上游 calt 的脚本可达范围，横排第二字改用补足严格二字宽的延续字形；
竖排统一使用同一个 uniFE31，并由 GPOS 定位第二字。审计同时校验 calt、
vert、vrt2、两种 lookup 执行顺序和 FreeType 位图笔画一致性。
对于 exact 静态样式，非数字/非冒号码位会保留上游 simple glyph flags、
glyf bbox 和组合字形结构；Noto chws/vchw 后处理结束后还会再次恢复可直接
对齐字形的参考 bbox，避免 1 unit 重算通过 phantom points 改变 hinted 位图。
静态 TTF 与上游一样使用 post format 3，不在
字体中存储 glyph names；数字的默认比例宽/tnum 等宽关系由 cmap 与 GSUB
表达，不再为显示名称改变 glyph order 或制造与 GID 不一致的自动名。最终写出 glyf
时保留上游 OVERLAP_SIMPLE 语义，并用 OTS 可接受的 repeat 编码保存重复
overlap flags，而不是清除 bit 6。unhinted 套件中的 OTS maxZones/gasp 警告
继承自上游 unhinted 基线，返回码为 0。
glyph 总数不强行补齐到与上游一致；cmap 字形和布局可达的未编码字形会保留，
不可达 glyph 数量差异视为构建产物。
这些字体是修改派生版，不是 Sarasa Gothic、Source Han Sans 或 Inter 的官方发布。
