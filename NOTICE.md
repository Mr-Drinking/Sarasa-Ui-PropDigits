# 声明

本仓库包含基于 Sarasa Gothic、Source Han Sans 和 Inter 修改得到的字体文件。

版权声明：

- Copyright (c) 2015-2025, Renzhi Li (aka. Belleve Invis, belleve@typeof.net)。
- Portions Copyright (c) 2016 The Inter Project Authors。
- Portions Copyright (c) 2014-2021 Adobe Systems Incorporated，Reserved Font Name 为 `Source`。
- Portions Copyright (c) 2012 Google Inc.。
- CL 的传统旧字形覆盖来自 Shanggu Sans `1.028` 的官方静态 TTF 和 `ShangguSansTC-VF.ttf`。Shanggu Fonts 项目按 SIL Open Font License 1.1 分发。

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

- 发布审计在六地区、六个命名字重上比较全部 ideograph cmap，共覆盖 184,302 个“地区 × 码位”组合和 1,105,812 个“地区 × 码位 × 字重”轮廓实例。正斜体 cmap 与轮廓拓扑必须一致；以全部轮廓点求出最小最大残差的整字平移后，每点相对 9.4° 剪切结果的残差不得超过 2.0 font units。替换前的旧 CL VF 在 wght=200 会被检出 7,968 / 30,717 个异常、最大残差约 53.92 units；最终输出为 0，六地区最大残差为 1.692365 units。面积守恒、字重单调性和墨量跨度仍独立检查。
- 可变字体系列直接合并对应地区的 CJK VF 和 Inter VF，不从静态字重派生或插值。SC/TC/HC/J/K 使用对应的 Source Han Sans VF；CL 以 `SourceHanSansK-VF` 为底稿，并使用 Shanggu Sans `1.028` 的 `ShangguSansTC-VF` 覆盖传统旧字形。
- CJK Italic VF 由对应地区的 CJK 正体 VF 以 `9.4°` 仿斜生成。构建先在未剪切坐标中展开全部 `gvar` IUP 隐含增量，再剪切基础轮廓和显式 delta；四个 metric phantom points 保持不变。直接剪切稀疏 delta 会改变 IUP 插值并在非默认字重产生局部笔画墨量突变，因此不再采用该做法。
- 静态 TTF 不从 VF 实例化；hinted 与 unhinted 两套都使用对应地区的静态 Source Han Sans 和静态 Inter，按 Sarasa 上游的 `pass1`、`kanji`、`hangul`、`pass2` 片段流程构建。CL 在 `kanji` 阶段使用 Shanggu Sans `1.028` 官方 `ShangguSansTC-*.ttf` 覆盖传统旧字形，以保持 CL 静态和 VF 都跟随同一 Shanggu 发布口径；最终静态 CL 的公开 cmap、GSUB/GPOS feature 边界和非数字 metrics 仍按官方 SarasaUiCL 参考字体裁剪与同步。
- hinted 静态 TTF 会对本项目实际生成的片段重新 hint。每个字重固定使用与 Sarasa 上游相同顺序的完整分析环境：8 个家族、6 个地区、正斜体共 96 个 `pass1`，另有 6 个 `kanji` 和 6 个 `hangul`，全部高层 hint 最后按上游 `GroupInstr` 顺序交给一次统一 `instruct`。高层 hint store 的 glyph 项按数字 GID 规范化，`sharedHints` 则保留 Sarasa hcfg 的 `Ideograph`、`Hiragana`、`Katakana` 语义顺序；其位置决定 TrueType 函数 ID，因此不能按名称排序。ExtraLight、Light、Regular、SemiBold、Bold 使用上游同名 reference/hcfg；Heavy 的 Ui 与 FE 使用实际 900 轮廓，只有其辅助拉丁环境和 hcfg 取 Bold 边界，不复制相邻成品的 glyph instructions。缓存以完整字重分组为单位，只保存高层 hint 数据，并把该顺序规则纳入缓存版本；指定地区构建和断点恢复也不会缩小分析环境。unhinted 静态 TTF 是正式的无 TrueType instructions 输出。
- VF、hinted 静态 TTF 和 unhinted 静态 TTF 都保留 `STAT`。静态 `STAT` 只描述单个静态实例的 weight/italic 样式，不表示静态文件仍有可变轴。
- `OS/2.achVendID` 使用 `MRDK` 标识本仓库修改版，不沿用上游 Sarasa Ui 的 `????` 占位值，也不表示这些字体是 Source Han Sans 或 Inter 的官方版本。
- VF 的 nameID 25 使用只含 ASCII 字母数字的 Variations PostScript Name Prefix。从 Inter 导入 `cv14` 时同步或重映射其 FeatureParams 引用的界面名称记录，避免悬空 name ID 并保证 TTX roundtrip 可解析。
- `head.fontRevision` 使用 OpenType 16.16 fixed 数值 `1.0403` 对应本仓库版本 `1.0.40.3`；nameID 5 以 `Version 1.0403; project 1.0.40.3; ...` 开头，使首个 OpenType `major.minor` 数值与 `head` 一致，同时保留完整发布版本。版本号中的 `1.0.40` 是 Sarasa Gothic 上游基线，最后的 `.3` 是本仓库衍生版本号。
- 与上游 Sarasa Ui 的有意差异是默认 ASCII 数字为变宽数字、公开字重级别采用 Sarasa/CSS 口径 `200/300/400/600/700/900`，VF 与静态 TTF 都使用与 Inter 一致的上下文冒号 colon-run `calt`，中文 `——` 在横排和竖排都严格占 `2em`，以及 CL 跟随 Shanggu Sans `1.028` 官方 TTF/VF 发布物而不是 Sarasa `1.0.40` 内置旧 subset 转换产物。这里的“官方上游”同时包括 Sarasa Ui 和 Source Han Sans：单个 U+2014 保留原比例宽、轮廓和 glyph；静态成对输入保留官方 Sarasa Ui 的 `calt` 续接结构，并以补数 advance 补足二字宽；VF 则采用官方 Source Han Sans 静态/VF 均使用的 `ccmp` 双/三连长字形与 `vert` 竖排替换结构，以单 glyph 消除可变 advance 独立取整产生的 `1999/2001` units。VF 新增长字形在六个公开字重使用本系列对应静态 TTF 的组合轮廓作为控制点，中间位置由 `gvar` 连续插值；仅开启 `vrt2`、同时关闭 `vert` 时保留两个 `uniFE31`，与两个上游的边界行为一致。其中 public ExtraLight `200` 的 CJK 来源是 Source Han Sans ExtraLight `250`；public SemiBold `600` 的 CJK 来源是 Source Han Sans Medium `500`，Latin 来源是 Inter SemiBold `600`。隐藏的 public `350` 只作为 `avar` 插值锚点，不作为命名实例或静态样式。Heavy `900` 使用 Source Han Heavy `900` 与 Inter Black `900`，优先保持两个上游的终点语义和轴单调性；极粗端的中西文视觉墨量可能不完全相同。Shanggu 比官方 SarasaUiCL 多出的码位或旧字形 feature 不会额外暴露到静态 CL 公开字体里。
- VF 合并必须先把 Source Han 切换到 public 轴，再把 Inter `gvar` support 钉到同数字 public 坐标；构建脚本对该调用顺序硬失败。否则 public `600` 会错误地得到 Inter Medium `500`，并只在 600 附近表现为 VF/静态不一致。发布审计会实际实例化并分别对照 Source Han internal `500` 与 Inter `600`，不只检查 fvar/STAT 名称。
- 静态 TTF 与上游一样使用 `post` format 3，不保存 glyph names；比例/等宽数字关系由 cmap/GSUB 表达，不改变 glyph order。VF 与静态成品均写入 Sarasa、Inter、Adobe、Google 四方版权；Heavy 不设置 legacy Bold 位；静态 codepage bit 31 在 subset 后重新确认；VF 用 MVAR 对齐六个静态字重的下划线指标。
- 最终字体保留 `ccmp`、裁剪到上游 Sarasa Ui 覆盖范围的 `locl`、Hangul Jamo、`vert/vrt2`、数字特性，以及上游 Sarasa Ui 暴露的空 `cv/ss` FeatureRecord。静态 TTF 的 GSUB Script / LangSys 顺序和基础 GPOS FeatureRecord/LookupList 结构按对应地区参考字体同步，`palt` 下假名等已有 glyph 的定位值也按参考字体同步；VF 同步 GDEF class/mark 模板时保留 Source Han ItemVariationStore，使 `kern` / `palt` / `vpal` 的原有 GPOS VariationIndex 不会悬空。Source Han 静态与 VF 发布物的 `palt/vpal` 定位数据并非处处相同，因此静态输出按 Sarasa 静态参考同步，VF 输出保留 Source Han VF 的可变数据；这项上游路径差异不强行抹平。静态连续 U+2014 保留 Sarasa `calt` 的脚本可达范围并使用第二字补数 advance；VF 在最终模板后追加 Source Han 式 `ccmp` 双/三连长字形和 `vert` 映射，破折号变化只写入 glyf/gvar，不追加自定义 GPOS/GDEF VariationIndex。VF 审计在 13 个轴位置检查几何和多 ppem 位图，并以 `0.5` 字重步长遍历整条公开轴的 1401 个位置，对默认横排、显式 `ccmp`、默认竖排、显式 `vert`、`vrt2-only`、`vert+vrt2` 共执行 8406 次 shaping；六个公开字重还与对应静态字体比较组合边界、advance 和无 hint 位图。GSUB lookup 内容还保留本派生项目新增的 PropDigits 冒号上下文规则。随后按照 Noto CJK 官方交付流程追加 GPOS `chws/vchw`，分别实现横排和竖排连续全角标点的上下文压缩。该特性固定使用 `chws_tool 1.4.5` 与 `east-asian-spacing 1.4.5`，只新增 GPOS FeatureRecord/contextual positioning lookup，不修改 cmap、轮廓、hint、advance、GSUB、GDEF 或公开字重轴。VF 的基础 GPOS lookup 结构可能因 VF 合并路径与静态官方字体不同，但发布审计会检查 VF 压力实例、cmap、hmtx/vmtx、数字/冒号、`chws/vchw` 和 exact-weight metrics。静态 TTF 最终写出时保留上游 `OVERLAP_SIMPLE` 语义，并用 OTS 可接受的 repeat 编码保存重复 overlap flags；unhinted 的 OTS `maxZones/gasp` 警告继承自上游基线。
- Noto `chws/vchw` 后处理会重新序列化静态 `glyf`。为避免约三万个 Italic 映射码位的 1 unit bbox 重算改变 TrueType phantom points，构建会在后处理完成后恢复可直接对齐字形的官方 Sarasa Ui bbox；最终 60 个 exact hinted 案例、13,884,300 次 FreeType 位图比较均为 0 差异，不需要重新运行 hint 分析。静态 `palt` 定位值则按展开轮廓结构与横竖 metrics 建立语义映射，不使用 `post` format 3 产生的跨字体不稳定自动名；120 个 exact 样式案例全部对齐。
- `chws/vchw` 是来自 Noto CJK 交付构建的特性。Source Han Sans `2.005R` 底稿及 Sarasa Gothic `1.0.40` 参考字体本身不含这两个 FeatureRecord；Noto CJK 与 Source Han Sans 共用 CJK 字形源，但在发布阶段另行执行 `add-chws` 后处理。本项目采用最新版工具实现同一特性，不声称它由 Adobe 的 Source Han Sans 二进制原生提供。
- 相对 `v1.0.40`，构建环境升级到 Node.js `26.7.0`、`uharfbuzz 0.56.0`、`ttfautohint-py 0.6.1` 和 `py7zr 1.1.3`。Node 仍执行 Sarasa `1.0.40` 的固定 lockfile；`ttfautohint-py 0.6.1` 未更换 hint 算法；`py7zr` 只负责解包；HarfBuzz 升级后的 `chws/vchw` GPOS 已与旧核心结果对照一致。Source Han Sans VF、Inter VF、Shanggu、Sarasa 参考包与按平台选择的 Node.js 归档都使用固定 SHA-256 校验。
- 发布前使用 OTS `9.3.0` 复核全部 156 个字体，返回码全部通过；72 个 unhinted 静态 TTF 各出现一次与官方 unhinted Sarasa Ui 相同的 `maxZones/gasp` 信息，72 个 hinted 静态 TTF 与 12 个 VF 没有该信息。FontBakery `1.1.0` 的 `opentype/font_version` 对全部 156 个项目字体均为 PASS；SC Regular 静态与正斜体 VF 的完整代表性检查均为 0 FAIL，剩余 GDEF mark、spacing mark、`post` format 3 和 `xAvgCharWidth` 四类 WARN 可在对应官方 Sarasa Ui 样本中复现，其中 GDEF mark 与 `post` 项也存在于官方 Source Han Sans SC VF。官方 Sarasa Ui 自身继承的 `font_version` mismatch 没有保留到本项目。内部审计在六个命名字重上遍历六地区最终 VF 的全部 ideograph cmap，共覆盖 184,302 个“地区 × 码位”组合和 1,105,812 个轮廓实例；另完成 120 个静态 exact、144 个静态 layout、120 个 `palt`、120 个 PropDigits、144 个静态破折号、60 个 VF exact metric、12 个 VF 源配对、12 条连续字重曲线、156 个 VF 破折号和 72 个 `chws/vchw` 案例，所有 failure 计数均为 0。
