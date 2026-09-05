# 声明

本仓库包含基于 Sarasa Gothic、Source Han Sans 和 Inter 修改得到的字体文件。

版权声明：

- Copyright (c) 2015-2025, Renzhi Li (aka. Belleve Invis, belleve@typeof.net)。
- Portions Copyright (c) 2016 The Inter Project Authors。
- Portions Copyright (c) 2014-2021 Adobe Systems Incorporated，Reserved Font Name 为 `Source`。
- Portions Copyright (c) 2012 Google Inc.。
- © 2022-2026 Shanggu Fonts。此项适用于 CL 的传统旧字形覆盖；来源为 Shanggu Sans `1.028` 官方静态 TTF 和 `ShangguSansTC-VF.ttf`。

字体按 SIL Open Font License 1.1 分发，见 [LICENSE](LICENSE)。

本仓库中的修改版字体家族：

- `Sarasa Ui VF PropDigits SC`
- `Sarasa Ui VF PropDigits CL`
- `Sarasa Ui VF PropDigits TC`
- `Sarasa Ui VF PropDigits HC`
- `Sarasa Ui VF PropDigits J`
- `Sarasa Ui VF PropDigits K`
- `Sarasa Ui PropDigits CL`
- `Sarasa Ui PropDigits SC`
- `Sarasa Ui PropDigits TC`
- `Sarasa Ui PropDigits HC`
- `Sarasa Ui PropDigits J`
- `Sarasa Ui PropDigits K`

这些字体不是上游官方发布。请不要将其表述为 Sarasa Gothic、Source Han Sans 或 Inter 的官方版本。

构建说明：

- 静态 TTF 使用 Sarasa 的原生静态片段流程；VF 合并对应 CJK 与 Inter 可变来源。CL 使用 Source Han K 和 Shanggu TC，公开字符集及布局保持 Sarasa Ui CL 边界。
- 公开字重为 200、300、400、600、700、900。350 是隐藏锚点；600 的 CJK/Latin 来源分别为 Source Han 500 与 Inter 600。
- Inter 预设按 Sarasa 顺序通过 cmap 烘焙，保持替代字形的定位与组件身份。Inter 和 CJK 的 GDEF 变化数据、mark filtering sets 及布局引用一并处理。
- 默认比例数字，tnum/pnum 负责宽度切换，冒号遵循 Inter colon-run calt。六地区无语言标记时采用各自地区的标点语义；显式英文和各地区本地化行为见 README。
- chws/vchw 源于 Noto CJK 交付后处理，具体应用是否自动启用需要实测。
- Italic VF 先物化 IUP 再剪切，metric phantom points 不剪切；TrueType VORG 被移除，静态竖排原点由最终 glyf/vmtx 决定，VF 使用明确的 metric phantom 变化数据。元数据与布局修订不重新 hint。
- 固定版本、归档 SHA-256、解包缓存、Python 私有环境及 Node 运行时均由正式构建入口准备和核验。
- 发布前必须完成完整主审计、156 个字体的 OTS、按地区运行的 FontBakery 318 PASS、视觉检查与 21 个 ZIP 校验。报告绑定当前字体哈希，使用可移植路径。

具体来源、构建参数及对应版本的审计结果见 README 和 reports 目录。
