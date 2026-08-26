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

- 发布审计在六地区、六个命名字重上比较全部 ideograph cmap，共覆盖 184,302 个“地区 × 码位”组合和 1,105,812 个“地区 × 码位 × 字重”轮廓实例。正斜体 cmap 与轮廓拓扑必须一致；以全部轮廓点求出最小最大残差的整字平移后，每点相对 9.4° 剪切结果的残差不得超过 2.0 font units。替换前的旧 CL VF 在 wght=200 会被检出 7,968 / 30,717 个异常、最大残差约 53.92 units；最终输出为 0，六地区最大残差为 1.692365 units。面积守恒、字重单调性和墨量跨度仍独立检查。
- 可变字体系列直接合并对应地区的 CJK VF 和 Inter VF，不从静态字重派生或插值。SC/TC/HC/J/K 使用对应的 Source Han Sans VF；CL 以 `SourceHanSansK-VF` 为底稿，并使用 Shanggu Sans `1.028` 的 `ShangguSansTC-VF` 覆盖传统旧字形。
- CJK Italic VF 由对应地区的 CJK 正体 VF 以 `9.4°` 仿斜生成。构建先在未剪切坐标中展开全部 `gvar` IUP 隐含增量，再剪切基础轮廓和显式 delta；四个 metric phantom points 保持不变。直接剪切稀疏 delta 会改变 IUP 插值并在非默认字重产生局部笔画墨量突变，因此不再采用该做法。
- 拉丁静态 TTF 跟随 Sarasa 的静态 Inter 片段路径，拉丁 VF 则直接跟随 Inter VF；两条上游发布路径已有合理定点轮廓差异，Italic 复合字形还受组件位置和 `USE_MY_METRICS` 影响。本项目不强制两者完整 bbox 相同；Inter 轮廓在 public `200..900` 的 7 个设计控制点和 6 个中间探测点按允许整字平移的方式核验，六个发布字重的横竖 advance、LSB、TSB 则由 HarfBuzz 对项目 unhinted 静态字体逐码位要求 exact。
- 静态 TTF 不从 VF 实例化；hinted 与 unhinted 两套都使用对应地区的静态 Source Han Sans 和静态 Inter，按 Sarasa 上游的 `pass1`、`kanji`、`hangul`、`pass2` 片段流程构建。CL 在 `kanji` 阶段使用 Shanggu Sans `1.028` 官方 `ShangguSansTC-*.ttf` 覆盖传统旧字形，以保持 CL 静态和 VF 都跟随同一 Shanggu 发布口径。最终静态 CL 的公开 cmap 与 GSUB/GPOS 模板按 SarasaUiCL 参考裁剪；五个官方同名字重同步非数字 metrics，Heavy 保留 Shanggu Heavy 来源数据。
- hinted 静态 TTF 会对本项目实际生成的片段重新 hint。每个字重固定使用与 Sarasa 上游相同顺序的完整分析环境：8 个家族、6 个地区、正斜体共 96 个 `pass1`，另有 6 个 `kanji` 和 6 个 `hangul`，全部高层 hint 最后按上游 `GroupInstr` 顺序交给一次统一 `instruct`。高层 hint store 的 glyph 项按数字 GID 规范化，`sharedHints` 则保留 Sarasa hcfg 的 `Ideograph`、`Hiragana`、`Katakana` 语义顺序；其位置决定 TrueType 函数 ID，因此不能按名称排序。ExtraLight、Light、Regular、SemiBold、Bold 使用上游同名 reference/hcfg；Heavy 的 Ui 与 FE 使用实际 900 轮廓，只有其辅助拉丁环境和 hcfg 取 Bold 边界，不复制相邻成品的 glyph instructions。缓存以完整字重分组为单位，只保存高层 hint 数据，并把该顺序规则纳入缓存版本；指定地区构建和断点恢复也不会缩小分析环境。unhinted 静态 TTF 是正式的无 TrueType instructions 输出。
- VF、hinted 静态 TTF 和 unhinted 静态 TTF 都保留 `STAT`。静态 `STAT` 只描述单个静态实例的 weight/italic 样式，不表示静态文件仍有可变轴。
- `OS/2.achVendID` 使用 `MRDK` 标识本仓库修改版，不沿用上游 Sarasa Ui 的 `????` 占位值，也不表示这些字体是 Source Han Sans 或 Inter 的官方版本。
- VF 的 nameID 25 使用只含 ASCII 字母数字的 Variations PostScript Name Prefix。从 Inter 导入 `cv14` 时同步或重映射其 FeatureParams 引用的界面名称记录，避免悬空 name ID 并保证 TTX roundtrip 可解析。
- `head.fontRevision` 使用 OpenType 16.16 fixed 数值 `1.0403` 对应本仓库版本 `1.0.40.3`；nameID 5 以 `Version 1.0403; project 1.0.40.3; ...` 开头，使首个 OpenType `major.minor` 数值与 `head` 一致，同时保留完整发布版本。版本号中的 `1.0.40` 是 Sarasa Gothic 上游基线，最后的 `.3` 是本仓库衍生版本号。
- 与上游 Sarasa Ui 的有意差异是默认 ASCII 数字为变宽数字、公开字重级别采用 Sarasa/CSS 口径 `200/300/400/600/700/900`，VF 与静态 TTF 都使用与 Inter 一致的上下文冒号 colon-run `calt`，破折号统一跟随 Source Han/Shanggu 的 `ccmp → locl → vert/vrt2` 结构，以及 CL 跟随 Shanggu Sans `1.028` 官方 TTF/VF 发布物而不是 Sarasa `1.0.40` 内置旧 subset 转换产物。SC/TC/HC/J/K 的非 CJK 路径保留比例 U+2014 与比例 U+2E3A/U+2E3B；JAN/ZHH/ZHS/ZHT 切换到严格 `1em/2em/3em` 字形，KOR 单字使用 Source Han 较窄且位置较高的地区字形，双连和三连仍严格为 `2em/3em`。CL 按 Shanggu 全局共用全宽 U+2014/U+2015 和严格二、三字宽长字形，不伪造 Source Han 地区 `locl`。正常横竖路径使用一个长 glyph；只启用 `vrt2` 的上游边界可以保留多个相同竖排单字形。这些破折号轮廓并非跨字重固定：笔画厚度、少量 side bearing 和比例 advance 随 Source Han/Shanggu 字重变化，中宫位置及 CJK `1em/2em/3em` 语义保持稳定。public ExtraLight `200` 的 CJK 来源是 Source Han Sans ExtraLight `250`；public SemiBold `600` 的 CJK 来源是 Source Han Sans Medium `500`，Latin 来源是 Inter SemiBold `600`。隐藏的 public `350` 只作为 `avar` 插值锚点，不作为命名实例或静态样式。Heavy `900` 使用 Source Han Heavy `900` 与 Inter Black `900`。Shanggu 比官方 SarasaUiCL 多出的码位或旧字形 feature 不会额外暴露到静态 CL 公开字体里。
- 省略号保留 Source Han/Shanggu 的三种上游语义：拉丁文字上下文（`Latn/en`）下 U+2026 使用 Sarasa Ui 烘焙 `pwid` 后的下沉比例字形；CJK 文字上下文中的 JAN/KOR/ZHH/ZHS/ZHT `locl` 切换到居中的 `1em` 全宽字形，连续两个 U+2026 保持两个 glyph 并严格占 `2em`；`vert/vrt2` 切换到现成的竖排字形。该修复只增加 GSUB 路由，复用字体内已有轮廓、metrics 与 hint，不把省略号做成连字，也不重新 hint。中文需由 shaping 引擎传入 `Hani/zh-Hans`、`Hani/zh-Hant` 或 `Hani/zh-HK`，日文与韩文分别使用 `Hani/ja`、`Hani/ko`；缺少对应 CJK script/language 上下文时按非 CJK 默认路径显示。
- VF 的破折号与省略号源轮廓在审计前先经过一次 SFNT 写入/读取，以相同定点存储口径比较；横竖 advance 另由 HarfBuzz 直接读取成品和真实 Source Han/Shanggu VF 的 HVAR/VVAR，并要求整数 exact。Italic 为对齐官方 Sarasa 静态度量产生的整字 LSB 平移作为具名观察项保留，平移后轮廓及六个发布字重对项目静态字体的逐码位 side bearing 仍必须通过硬门槛。
- VF 合并必须先把 Source Han 切换到 public 轴，再把 Inter `gvar` support 钉到同数字 public 坐标；构建脚本对该调用顺序硬失败。否则 public `600` 会错误地得到 Inter Medium `500`，并只在 600 附近表现为 VF/静态不一致。发布审计会在 public `200/300/350/400/600/700/900` 对照同数字 Inter 轮廓，在 `250/325/375/500/650/800` 增加中间探测，并在 public `600` 另对照 Source Han internal `500` 的 CJK 轮廓与轴坐标，不只检查 fvar/STAT 名称。
- 静态 TTF 与上游一样使用 `post` format 3，不保存 glyph names；比例/等宽数字关系由 cmap/GSUB 表达，不改变 glyph order。VF 与静态成品均写入 Sarasa、Inter、Adobe、Google 的版权，CL 另写入 Shanggu Fonts 原版权；nameID 13/14 直接携带 OFL 1.1 说明和链接。Heavy 不设置 legacy Bold 位；静态 codepage bit 31 在 subset 后重新确认；VF 用 MVAR 对齐六个静态字重的下划线指标。hinted/unhinted 静态 nameID 3/5 明确区分变体，最终 name table 删除全部 platform 1（Macintosh）记录，Windows/Unicode 本地化名称和版权不变。
- 最终字体保留 `ccmp`、裁剪到上游 Sarasa Ui 覆盖范围的 `locl`、Hangul Jamo、`vert/vrt2`、数字特性，以及上游 Sarasa Ui 暴露的空 `cv/ss` FeatureRecord。静态 TTF 的 GSUB Script / LangSys 顺序和基础 GPOS FeatureRecord/LookupList 结构按对应地区参考字体同步；破折号地区映射优先原位扩展已有 `locl`，相应 LangSys 缺少可用记录时才按 ZHS/ZHT/ZHH/JAN/KOR 补充 FeatureRecord，CAT 空 `locl` 继续为空。旧 Sarasa `calt` continuation/pair-start 与破折号 GPOS PairPos 会被删除。静态逐字重复制 Source Han/Shanggu 对应破折号轮廓与 metrics，Italic 做统一 `9.4°` 剪切；VF 保留同一上游的 `gvar` 轮廓变化，并把来源破折号 HVAR/VVAR 变化转入 phantom-point variations。全部校正完成后，VF 再从 public `200/300/350/400/600/700/900` 七个控制实例重建整字体 HVAR/VVAR，覆盖 horizontal advance、LSB/RSB、vertical advance、TSB/BSB 和 vertical origin；默认轮廓同时满足 TrueType VF 的 LSB/xMin 与 `head.flags` 规范。Source Han/Shanggu 静态 TTF 与 VF 发布路径的破折号可能有整字平移、side bearing 和 1 unit 量化差异；本项目分别逐角色对齐各自来源，跨产品强制相同的单 glyph 与 `1em/2em/3em` 语义，并在审计中公开 exact bounds/位图观察项。静态最终坐标替换保持四点矩形拓扑并沿用同一成品已生成的 glyph program，不重新运行整组高层 hint 分析；发布审计逐角色检查 instructions、轮廓和多 ppem 栅格。VF 同步 GDEF class/mark 模板时保留 Source Han ItemVariationStore，使 `kern` / `palt` / `vpal` 的原有 GPOS VariationIndex 不会悬空。五个官方同名字重的静态 `palt` 按 Sarasa 静态参考同步，Heavy 按同次 pass2 Heavy/Black 来源同步；VF 保留 Source Han VF 的可变数据。随后按照 Noto CJK 官方交付流程追加 GPOS `chws/vchw`；它们只新增 6 个 contextual positioning lookup，不修改 cmap、轮廓、hint、advance、GSUB、GDEF 或公开字重轴。静态 `glyf` 写出时保留首点 `OVERLAP_SIMPLE` 语义；重复 flag 优先编码为首 flag 的 OTS-compatible repeat run，坐标替换使压缩位不同而无法共用 repeat 时，只清除后续点上无语义且被 OTS 禁止的显式重复 bit 6。48 个 Italic TTF 共规范化 100 个 glyph，修复前后 1,600 次定点位图比较一致。unhinted 将 `maxp.maxZones` 规范为 `1`、`gasp` 末端规范为 `0xFFFF`，不加入 instructions。
- VF 的 LSB/xMin 校正 variation 只平移真实轮廓点，四个 metric phantom points 保持零增量；同时移动 phantom points 会抵消可见轮廓平移，使 FontTools 实例化看似正常而 HarfBuzz 仍返回错误 side bearing。最终 HVAR/VVAR 以本项目 unhinted 静态成品为命名字重基线，并由 HarfBuzz 在六个命名字重与隐藏 `350` 上逐 cmap 验证横竖 advance、LSB 和 TSB。静态最终化只允许 `head/maxp/OS/2/name/gasp/GSUB` 变化，其他 SFNT 表逐表核对 SHA-256；省略号因此不触发重新 hint。
- Italic VF 在非默认字重的 Inter 复合字形曾存在组件相对位置漂移。构建现在先物化继承的上游 `gvar`，完成 LSB/xMin 与 HVAR/VVAR 校准后，直接校正默认 `400` 的基础复合组件，再只为 12 个非默认探测点中实际超出 2 units 的字形追加组件 tuple；简单字形、组件数量或变换不匹配都会硬失败。旧 SC Italic 在 public `300/350/600/700` 分别有 `138/139/139/139` 个 mismatch，`200/400/900` 也各有 136 个；新流程的 13 点均为 0。SC 正式构建直接校正 136 个默认复合字形，只需 40 条非默认 tuple、2 条 1-unit 边距回填和 18 条 HarfBuzz 整字平移，最大平移 6 units。随后按组件依赖顺序恢复决定 LSB/TSB 的 `xMin/yMax`，并用只做整字平移的 HarfBuzz 校准消除定点边距回归；整字平移不改变 Inter 相对轮廓。该修复只重建六地区 Italic VF，不改静态 TTF，也不重新 hint。
- 默认 VF 轮廓平移按组合字形依赖从组件到父字形排序并逐层重算 bbox，避免共享组件与父字形被重复平移。`gvar` 坐标长度只取最终 `glyf` 点数，不读取可能陈旧的 tuple 长度；写出前物化并重编译全部 tuple，原始越界点号或最终长度不符均作为失败。六个发布字重的 HarfBuzz metrics 必须与项目静态字体 exact；隐藏 `350` 的 FontTools/HarfBuzz 交叉检查只允许并显式记录水平不超过 2 units、垂直不超过 5 units 的 side-bearing 定点量化，missing glyph、advance 差异或任一方向超限仍失败。
- Noto `chws/vchw` 后处理会重新序列化静态 `glyf`。为避免约三万个 Italic 映射码位的 1 unit bbox 重算改变 TrueType phantom points，构建会在后处理完成后恢复可直接对齐字形的官方 Sarasa Ui bbox；最终完整审计覆盖 72 个静态栅格案例、16,659,660 次 FreeType 渲染，全部为 0 差异，不需要重新运行 hint 分析。静态 `palt` 定位值则按展开轮廓结构与横竖 metrics 建立语义映射，不使用 `post` format 3 产生的跨字体不稳定自动名；144 个静态样式案例全部通过。
- `chws/vchw` 是来自 Noto CJK 交付构建的特性。Source Han Sans `2.005R` 底稿及 Sarasa Gothic `1.0.40` 参考字体本身不含这两个 FeatureRecord；Noto CJK 与 Source Han Sans 共用 CJK 字形源，但在发布阶段另行执行 `add-chws` 后处理。本项目采用最新版工具实现同一特性，不声称它由 Adobe 的 Source Han Sans 二进制原生提供。
- 相对 `v1.0.40`，构建环境升级到 Node.js `26.7.0`、`uharfbuzz 0.56.0`、`ttfautohint-py 0.6.1` 和 `py7zr 1.1.3`。Node 仍执行 Sarasa `1.0.40` 的固定 lockfile；`ttfautohint-py 0.6.1` 未更换 hint 算法；`py7zr` 只负责解包；HarfBuzz 升级后的 `chws/vchw` GPOS 已与旧核心结果对照一致。若系统 Python 已有完整精确版本就直接复用，否则只在 `.build-cache/python-venv` 私有环境安装固定 requirements。Source Han Sans VF、Inter VF、Shanggu、Sarasa 参考包、Sarasa 源码 ZIP 与按平台选择的 Node.js 归档都使用固定 SHA-256 校验，所有归档成员在解包前检查路径安全。
- 发布门禁使用显式审计节注册表；新增结果未接入门禁会硬失败，跳过 raster 的 checkpoint 只标为 `PARTIAL`，不能显示 `PASSED`。除 FontTools 实例化比较外，还以 HarfBuzz 直接读取 12 个 VF，在六个命名字重和隐藏 `350` 上逐 cmap 核验 horizontal/vertical advance 与 side bearing。命名字重的 FontTools 静态化 LSB/side-bearing 差异只有在同 case 的 HarfBuzz 全码位为 0、没有其他 metric 失败、普通字形水平不超过 2 units、垂直不超过 1 unit 时才降为观察；带 `USE_MY_METRICS` 的复合字形水平单列并限制为 16 units，原始计数、直方图和最大值完整保留。发布资产由 `tools/package_release.py` 固定成员顺序、时间戳、权限和压缩参数生成 21 个 ZIP；每包都含 LICENSE、中文 NOTICE/README，并输出 SHA256SUMS。
- 最终完整审计状态为 `PASSED`，全部门禁 failure 与 coverage failure 均为 0。72 个静态 raster case 共执行 16,659,660 次 FreeType 渲染比较，mismatch 为 0；VF 的 7 个 Inter 设计点和 6 个中间点分别完成 166,068 与 142,344 次轮廓比较，HarfBuzz 度量覆盖 3,886,848 个码位。独立 OTS `9.3.0` 对 `156/156` 个字体均只输出成功信息；FontBakery `1.1.0` 的正式分地区门共 318 项 PASS，包括 `font_version 156/156`、`no_mac_entries 156/156` 与成对 `STAT/ital_axis 6/6`。完整代表 profile 的上游/通用 CJK 基线和混合集合调用假失败继续在报告中单列。
