Sarasa Ui PropDigits SC 1.0.40.4（unhinted）

本包包含 200 ExtraLight、300 Light、400 Regular、600 SemiBold、700 Bold、900 Heavy
及对应 Italic，共 12 个静态 TTF。350 仅是 VF 隐藏锚点，不提供静态样式。

来源为 Source Han Sans SC 与 Inter 4.1，按 Sarasa 1.0.40 的 pass1/kanji/hangul/pass2
静态流程构建，不从 VF 实例化。600 配对 Source Han 500 和 Inter 600。
CL 的公开 cmap/layout 限于 Sarasa Ui CL 边界。

默认 ASCII 数字为比例宽；tnum 切换等宽，pnum 恢复比例宽。
冒号复用 Inter 的上下文规则，在 tnum 之前执行；1:2、1:、:2 上浮，
1:a、a:2、a:b 保持原位。tnum 与 zero 可同时启用。未提供语言标记时使用对应地区的全宽标点；明确的 Latn/en 保留英文省略号路径。KOR 单破折号保留地区特例。
破折号、省略号和竖排沿用对应 Source Han/Shanggu 字形，保持
ccmp → locl → vert/vrt2 顺序。中文双省略号为两个居中 glyph，共 2em；
中文双连、三连破折号分别为 2em、3em。CL 破折号全局保留 Shanggu 全宽形式。

chws/vchw 来自 Noto CJK 交付后处理；是否自动启用取决于实际应用。
静态竖排原点由最终 glyf/vmtx 决定，移除不适用于 TrueType 的 VORG。
post 使用 format 3，不保存虚构 glyph 名称。命名和布局修订不会重新 hint。

请阅读包根目录的中文 README.md、NOTICE.md 和 LICENSE.txt。
完整版本的主审计、OTS、分地区 FontBakery、视觉检查与 21 包校验报告见仓库 reports。
这些字体按 SIL Open Font License 1.1 分发，是修改版字体，不是任何上游的官方发布。
