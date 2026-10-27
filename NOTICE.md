# 声明

本仓库包含基于 Sarasa Gothic、Source Han Sans 和 Inter 修改得到的字体文件。

版权声明：

- Copyright (c) 2015-2025, Renzhi Li (aka. Belleve Invis, belleve@typeof.net)。
- Portions Copyright (c) 2016 The Inter Project Authors。
- Portions © 2014-2025 Adobe (http://www.adobe.com/)，Reserved Font Name 为 `Source`。此项与固定 Source Han Sans 2.005R 字体中的版权声明一致。
- Portions Copyright (c) 2012 Google Inc.。
- © 2022-2026 Shanggu Fonts。此项适用于 CL 的传统旧字形覆盖；来源为 Shanggu Sans `1.028` 官方静态 TTF 和 `ShangguSansTC-VF.ttf`。

字体按 SIL Open Font License 1.1 分发，见 [LICENSE](LICENSE)。

本仓库中的修改版字体家族：

- `Sarasa Ui VF PD SC`
- `Sarasa Ui VF PD CL`
- `Sarasa Ui VF PD TC`
- `Sarasa Ui VF PD HC`
- `Sarasa Ui VF PD J`
- `Sarasa Ui VF PD K`
- `Sarasa Ui PD CL`
- `Sarasa Ui PD SC`
- `Sarasa Ui PD TC`
- `Sarasa Ui PD HC`
- `Sarasa Ui PD J`
- `Sarasa Ui PD K`

这些字体不是上游官方发布。请不要将其表述为 Sarasa Gothic、Source Han Sans 或 Inter 的官方版本。

构建说明：

- 静态 TTF 使用 Sarasa 的原生静态片段流程；VF 合并对应 CJK 与 Inter 可变来源。CL 使用 Source Han K 和 Shanggu TC，公开字符集及布局保持 Sarasa Ui CL 边界。
- 公开字重为 200、300、400、600、700、900。350 是隐藏锚点；600 的 CJK/Latin 来源分别为 Source Han 500 与 Inter 600。
- Inter 先烘焙 tnum，再应用 ss03、cv10，最后对 37 个比例字符恢复 pnum；保留默认逗号、分号及替代 G 的定位规则。Inter 和 CJK 的布局按文字系统、语言和特性标签合并，GDEF 不通过位置性字形名匹配。
- 37 个数字、空格、标点和运算符默认使用 Inter 比例形态，tnum 恢复 tabular，pnum 不改变默认宽度。冒号遵循完整 Inter colon-run calt。西文按 Source Han 的语言规则排版；hani/kana/hang/bopo 的默认语言采用地区 CJK 形式，CL 中文破折号来自 Shanggu。
- chws/vchw 源于 Noto CJK 交付后处理，具体应用是否自动启用需要实测。
- Italic VF 先物化 IUP 再剪切，metric phantom points 不剪切；TrueType VORG 被移除，静态竖排原点由最终 glyf/vmtx 决定，VF 使用明确的 metric phantom 变化数据。元数据与布局修订不重新 hint。
- VF 横向原点补偿写入轮廓及组件坐标，保留 HVAR，竖向度量统一由 gvar 提供并移除 VVAR，避免 FreeType 与 HarfBuzz 采用冲突的原点路径。tnum 与 zero 组合共享等宽变化曲线。
- 构建、主审计环境与 FontBakery 环境分开准备并分别运行 pip check。视觉发布门同时校验逐图审阅状态、图片与生成器哈希、地区及字体覆盖。
- 本版固定 Sarasa Gothic 1.0.43。归档及解包文件、Git 源码、npm 文件清单与 Node 运行时核验固定来源或内容哈希。Python 安装使用完整哈希锁，已有环境按安装 RECORD 核验文件内容，成品校验记录同时绑定 Python 与依赖版本。
- 竖排连续破折号由 1em 片段组成，连接边一致，墨迹不越出各自 advance；轮廓在 hint 前生成，最终 head 包围盒按全部字形重算。
- head 汇总全部字形的边界，保留原生静态字形的保守包围盒，避免只改写 xMin 而改变度量原点。U+0360、U+22EF 及竖排连接片段使用显式原生 fallback 控制；CJK 与 Ui 的指令按完整输入组统一生成。
- VF 与 unhinted 静态字体保留启用 smart dropout 的最小 prep；unhinted 不含逐字形 hint、fpgm 或 cvt，不进行轮廓网格拟合。
- 成品先在暂存目录构建，完成验证后再替换；失败保留候选及原成品，替换中途错误会回滚。
- 发布前必须完成独立主审计（以 VF、静态字体与固定上游的差分比较为主干，不使用构建代码的判定）、156 个字体的 OTS、按地区运行的 FontBakery 630 PASS、视觉检查与 21 个 ZIP 校验。报告绑定当前字体哈希，使用可移植路径。

具体来源、构建参数及对应版本的审计结果见 README 和 reports 目录。
