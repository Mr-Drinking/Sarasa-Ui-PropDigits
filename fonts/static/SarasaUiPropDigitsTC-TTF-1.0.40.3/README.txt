Sarasa Ui PropDigits TC TTF 1.0.40.3

本目录包含静态 TrueType 字体。这些字体从静态 SourceHanSansTC 和
Inter 源字体出发，经 Sarasa 的 pass1/kanji/hangul/pass2 构建路径生成，
然后补上 PropDigits 派生行为。

TC 地区沿用 Sarasa 上游路径：CJK 底稿来自 SourceHanSansTC。

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
Heavy 使用 Source Han/Shanggu Heavy 900 与 Inter Black 900；Bold 只提供
布局、命名和 hint 配置边界，不覆盖 Heavy 的 glyph 数据。

公开字重采用 Sarasa/CSS 口径：ExtraLight 是 200。CJK 轮廓来源仍是
Source Han Sans 的 ExtraLight 口径 250；VF 通过轴映射让 public
wght=200 对应 Source Han 内部 wght=250，并让 public wght=600 对应
Source Han 内部 wght=500；Inter 在两个位置分别对应 200 和 600。

每个字重都包含正体和 Italic 文件。ASCII 数字默认使用比例宽度；
OpenType tnum 会恢复等宽数字，pnum 会把等宽数字切回比例数字。
静态 TTF 与 VF 使用一致的、与 Inter 兼容的 calt 冒号行为：
1:2 会上浮 ':'，1:a 和 a:2 不会上浮，1::2 等连续冒号上下文遵循
Inter 的 colon-run 规则。

破折号跟随 Source Han/Shanggu 的 ccmp、locl、vert/vrt2 结构。TC
在非 CJK 语言下保留比例 U+2014 和比例 U+2E3A/U+2E3B；CJK 地区标签
把它们切到严格 1em/2em/3em 的横竖字形。KOR 单字保留 Source Han
较窄且位置较高的地区字形；CL 则跟随 Shanggu，把 U+2014/U+2015
全局映射到同一全宽字形，不另造地区 locl。正常双连、三连路径各使用一个
长 glyph；仅显式启用 vrt2 的上游边界可能保留多个相同竖排单字形。
破折号笔画厚度、少量 side bearing 和比例 advance 会随字重变化，固定的是
CJK 的 1em/2em/3em 语义与中宫基线。Italic 使用对应正体轮廓的 9.4 度剪切。

省略号保留 Source Han/Shanggu 的语言语义：拉丁文字上下文（Latn/en）下，
U+2026 是下沉的比例字形；CJK 文字上下文中的 JAN/KOR/ZHH/ZHS/ZHT
locl 把它切换为居中的 1em 全宽字形，因此连续两个 U+2026 保持两个 glyph
并严格占 2em；vert/vrt2 再切换到现成的竖排字形。中文应传入 Hani/zh-Hans、
Hani/zh-Hant 或 Hani/zh-HK，日文与韩文分别使用 Hani/ja、Hani/ko；缺少
对应 CJK script/language 上下文时使用非 CJK 默认路径。该路由不新造轮廓、
不合成连字，也不重新 hint。

最终成品还会按 Noto CJK 的官方交付流程加入 GPOS chws/vchw：chws
用于横排连续全角标点的上下文压缩，vchw 用于对应的竖排压缩。实现固定使用
chws_tool 1.4.5 与 east-asian-spacing 1.4.5；Source Han Sans 2.005R
底稿本身不含这两个 FeatureRecord，因此它们在所有轮廓、hint、metrics 和
Sarasa layout 模板处理完成后统一追加。

name 表包含地区本地化显示名，例如：
更紗黑體 Ui PropDigits TC ExtraLight.
OS/2.achVendID 使用本派生项目的 MRDK，不继承上游 Sarasa Ui 的
???? 占位值。head.fontRevision 使用 OpenType fixed 数值 1.0403，
对应本仓库版本 1.0.40.3；nameID 5 以 OpenType 数值 Version 1.0403
开头，并在后续 project 字段保留完整版本 1.0.40.3。最终 name 表与官方
Sarasa/Source Han 成品一样不保留 platform 1（Macintosh）记录；Windows/Unicode
本地化名称以及 Sarasa、Inter、Adobe、Google 的版权保持完整，CL 另保留
Shanggu Fonts 的原版权声明。nameID 3/5 还会明确区分 hinted 与 unhinted，
避免系统把两套文件视为重复字体。
hinted 套件会对本项目实际生成的静态片段重新 hint。每个字重都固定
建立 Sarasa 上游顺序的完整环境：96 个 pass1 加 6 个 kanji 和 6 个
hangul，最后把全部 108 个输入交给一次统一 instruct。SemiBold 直接采用
上游同名环境；Heavy 的 Ui 与 FE 使用实际 900 轮廓，辅助拉丁环境和 hcfg
采用 Bold 边界，不复制相邻成品的 glyph instructions。
静态 PropDigits 会把 ':' remap
到已有的 pnum glyph，移除旧的冒号上下文替换，再追加与 Inter 一致的
colon-run calt 规则。
静态 TTF 保留静态 STAT 表，供现代应用识别 weight/italic 样式；这不会让
静态 TTF 变成可变字体。GSUB/GPOS 的 FeatureRecord 顺序、Script/LangSys
覆盖和基础 lookup 结构按对应样式的上游 Sarasa Ui TC 静态字体套模板；
随后追加 Noto CJK chws/vchw 的 FeatureRecord 和 contextual positioning lookup。
静态 TTF 最终会按对应 Sarasa Ui 参考字体裁剪 cmap；五个官方同名字重同步
非数字 metrics，Heavy 则保留同次 Sarasa pass2 Heavy/Black 的 hmtx/vmtx、
VORG、glyf 与 bbox。`palt` 下假名等已有 glyph 的定位值按展开轮廓结构与
横竖 metrics 建立语义映射：官方同名字重从 Sarasa 参考同步，Heavy 从同次
pass2 来源同步，不依赖 post format 3 产生的跨字体不稳定自动名。
破折号从对应字重的 Source Han/Shanggu 静态源复制 9 个核心语义字形，
SC/TC/HC/J/K 另复制 KOR 单字特例；各 CJK 地区标签优先在已有 locl
FeatureRecord 中原位扩展，模板没有对应 locl 时才补充一条，CAT 空 locl
继续保持为空。旧 calt continuation、pair-start 与竖排 PairPos 不会保留。
hinted 成品在相同四点矩形拓扑间替换坐标并保留已生成的 glyph program，
无需重新运行整组高层 hint 分析；审计逐角色核对 instructions、上游轮廓、
hmtx/vmtx、字重单调性和 FreeType 多 ppem 位图。
对于 exact 静态样式，非数字/非冒号码位会保留上游 simple glyph flags、
glyf bbox 和组合字形结构；Noto chws/vchw 后处理结束后还会再次恢复可直接
对齐字形的参考 bbox，避免 1 unit 重算通过 phantom points 改变 hinted 位图。
静态 TTF 与上游一样使用 post format 3，不在
字体中存储 glyph names；数字的默认比例宽/tnum 等宽关系由 cmap 与 GSUB
表达，不再为显示名称改变 glyph order 或制造与 GID 不一致的自动名。最终写出 glyf
时保留首点 OVERLAP_SIMPLE 语义，并优先用 OTS 可接受的首 flag repeat run
保存重复 overlap flag；坐标替换使 x/y 压缩位不同、无法共用 repeat 时，只清除
后续点上无语义且被 OTS 禁止的显式重复 bit 6。resume 与发布审计会解析原始
flag stream。unhinted 套件将 maxp.maxZones 规范为 1，并把 gasp 的最后范围
规范为 0xFFFF sentinel；这不会加入 TrueType instructions，也不会改变 glyf。
glyph 总数不强行补齐到与上游一致；cmap 字形和布局可达的未编码字形会保留，
不可达 glyph 数量差异视为构建产物。
这些字体是修改派生版，不是 Sarasa Gothic、Source Han Sans 或 Inter 的官方发布。
