Sarasa Ui PD K 1.0.42（hinted）

本包包含 200 ExtraLight、300 Light、400 Regular、600 SemiBold、700 Bold、900 Heavy
及对应 Italic，共 12 个静态 TTF。350 仅是 VF 隐藏锚点，不提供静态样式。

来源为 Source Han Sans K 与 Inter 4.1，按 Sarasa 1.0.42 的 pass1/kanji/hangul/pass2
静态流程构建，不从 VF 实例化。600 配对 Source Han 500 和 Inter 600。
CL 的公开 cmap/layout 限于 Sarasa Ui CL 边界。

37 个数字、空格、标点和运算符默认使用 Inter 比例形态；tnum 恢复 tabular，pnum 不改变默认宽度。
冒号复用 Inter 的上下文规则，在 tnum 之前执行；1:2、1:、:2 上浮，
1:a、a:2、a:b 保持原位。tnum 与 zero 可同时启用。拉丁、西里尔、希腊文字使用 Source Han 的西文语言路由；hani/kana/hang/bopo 的默认语言采用地区 CJK 形式。KOR 单破折号保留比例形态。
破折号、省略号和竖排沿用对应 Source Han/Shanggu 字形，保持
ccmp → locl → vert/vrt2 顺序。中文双省略号为两个居中 glyph，共 2em；
中文双连、三连破折号分别为 2em、3em；竖排拆为连续的 1em 片段，墨迹不越出各自 advance。
CL 中文破折号使用 Shanggu，西文使用 Source Han K。分段在 hint 前生成，最终 head 按全部字形重算。

chws/vchw 来自 Noto CJK 交付后处理；是否自动启用取决于实际应用。
静态竖排原点由最终 glyf/vmtx 决定，移除不适用于 TrueType 的 VORG。
post 使用 format 3，不保存虚构 glyph 名称。命名和布局修订不会重新 hint。
unhinted 仅保留启用 smart dropout 的最小 prep，不含逐字形 hint、fpgm 或 cvt。
家族名缩写为 PD，字重名称不缩写；Windows win 度量仍为上游 968/241，GDI 可能裁切部分超界字形。

请阅读包根目录的中文 README.md、NOTICE.md 和 LICENSE.txt。
完整版本的主审计、OTS、分地区 FontBakery、视觉检查与 21 包校验报告见仓库 reports。
这些字体按 SIL Open Font License 1.1 分发，是修改版字体，不是任何上游的官方发布。
