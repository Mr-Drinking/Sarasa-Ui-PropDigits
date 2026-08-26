# 更纱黑体 Ui PropDigits

<p align="center">
  <img
    src="assets/Sarasa_Ui_VF_Specimen.svg"
    alt="Sarasa Ui VF"
    title="Sarasa Ui VF"
  >
</p>

## 为什么要制作它？

曾几何时，我试图使用[更纱黑体（Sarasa Gothic）](https://github.com/be5invis/sarasa-gothic)作为 Android 手机的默认字体（因为个人更喜欢 [SF Pro](https://developer.apple.com/cn/fonts/) / [Inter](https://rsms.me/inter/) 而非 [Roboto](https://fonts.google.com/specimen/Roboto)），但由于修改软件只支持一个字体，我需要一个可变字体。但可惜的是，[原版更纱并不支持](https://github.com/be5invis/Sarasa-Gothic/issues/314)，我只好自己动手，用 Codex 中的 GPT-5.6 Sol 制作了本字体的可变版本；静态版本以及比例宽数字则是源于我对其成为 [Unigram](https://github.com/unigramdev/unigram) / Windows 默认字体的需要。

而到了 2026 年 8 月，随着上游、各个工具链（如 Node 版本）及我自身需求的更新，我用 GPT-5.6 Sol 将版本号刷到了 1.0.40（上游更新的是 Mono 版本，与本项目没有直接关系），同时添加了 `chws` / `vchw` 这两个 OpenType 特性，以方便在不能使用 CSS 的 `text-spacing-trim` 的情况下启用标点挤压。

## 简介

这个仓库包含 Sarasa Ui 的 PropDigits 多地区派生字体：

- **Sarasa Ui VF PropDigits CL / SC / TC / HC / J / K**：正体和 Italic 可变字体，公开 `wght` 轴为 `200..900`。
- **Sarasa Ui PropDigits CL / SC / TC / HC / J / K**：从静态 Source Han Sans、Inter 和必要的地区覆盖源按 Sarasa 静态片段路径构建的 TTF，包含 hinted 与 unhinted 两套，每套 6 个字重及对应 Italic。

CL 的传统旧字形覆盖跟随 [Shanggu Sans](https://github.com/GuiWonder/Shanggu) `1.028` 官方发布物：静态 TTF 使用 `ShangguSansTC-*.ttf`，VF 使用 `ShangguSansTC-VF.ttf`。这样静态和 VF 使用同一 Shanggu 发布口径，而不是绑定 Sarasa `1.0.40` 内置的旧 subset 转换产物。最终静态 CL 的公开 cmap 与 GSUB/GPOS 模板按官方 Sarasa Ui CL 边界裁剪；五个官方同名字重同步非数字 metrics，Heavy `900` 保留 Shanggu Heavy 来源数据。Shanggu 比官方 Sarasa Ui CL 多出的码位或旧字形 feature 不会额外暴露到本系列静态字体里。

两个系列都把 ASCII 数字 `U+0030..U+0039` 设为默认变宽数字，并提供 OpenType `tnum`/`pnum` 在变宽数字和等宽数字之间切换。VF 与静态 TTF 都按 Inter 的 `calt` 冒号行为处理数字冒号串：`1:2` 会上浮，`1:a`、`a:2`、`a:b` 不会上浮，`1::2`、`1:::a`、`a:::2` 等连续冒号上下文遵循 Inter 的 colon-run 规则。

静态 TTF 与 VF 现在使用同一套 Source Han/Shanggu 破折号机制，而不是分别保留 Sarasa `calt` 续接与 Source Han `ccmp` 两条路径。SC/TC/HC/J/K 在非 CJK 语言下保留比例 U+2014；`ccmp` 把连续两个、三个 U+2014 合成编码 U+2E3A/U+2E3B 的比例长字形。JAN/ZHH/ZHS/ZHT 的 `locl` 再把单字和长字形切到严格 `1em/2em/3em` 的全宽隐藏字形，KOR 的单字使用 Source Han 较窄且位置较高的地区字形，但双连和三连仍严格为 `2em/3em`；`vert/vrt2` 负责对应的竖排一、二、三字形。CL 跟随 Shanggu：U+2014 与 U+2015 全局共用全宽字形，U+2E3A/U+2E3B 也全局为严格二、三字宽，因此不额外伪造 Source Han 的地区 `locl`。正常横竖排的双连与三连均为一个 glyph；只启用 `vrt2`、同时关闭 `vert` 的上游边界可能保留两个或三个相同竖排单字形。

省略号沿用 Source Han/Shanggu 的三种既有字形语义。拉丁文字上下文（审计使用 `Latn/en`）下，U+2026 保留 Sarasa Ui 烘焙 `pwid` 后的下沉比例字形；CJK 文字上下文中的 JAN/KOR/ZHH/ZHS/ZHT `locl` 把它切换到 Source Han 默认的居中全宽字形，因此输入 `……` 仍是两个 glyph，合计严格占 `2em`；`vert/vrt2` 再切换到现成的竖排字形。这里不新造轮廓、不把两个字符做成连字，也不修改 hint。OpenType 的 `locl` 同时依赖 shaping 引擎传入 script 与 language：中文应使用 `Hani/zh-Hans`、`Hani/zh-Hant` 或 `Hani/zh-HK`，日文与韩文分别使用 `Hani/ja`、`Hani/ko`；缺少对应 CJK 上下文时按非 CJK 默认路径显示。

这些字形会随字重变化。Source Han/Shanggu 在不同字重上会改变笔画厚度、少量 side bearing 和比例字形 advance；保持稳定的是中宫位置以及 CJK 本地化后的 `1em/2em/3em` 语义。本项目静态字体逐字重复制对应静态上游字形，VF 则保留对应上游 `gvar` 与 metric 变化，并把 Source Han internal `250/300/350/400/500/700/900` 映射到 public `200/300/350/400/600/700/900`。

Source Han/Shanggu 的静态 TTF 与 VF 是两条官方发布路径，命名字重上的破折号也可能存在整字平移、side bearing 和 1 unit 量化差异，Italic 剪切后尤其明显。本项目不把其中一条改造成另一条：静态成品逐角色对齐静态源，VF 逐角色对齐 VF 源；跨产品严格要求相同的单 glyph 替换、`1em/2em/3em` advance 和地区语义，exact bounds 与位图差异则在报告中完整列为来源边界观察项。

拉丁部分也有同类边界：静态 TTF 跟随 Sarasa 的静态 Inter 片段路径，VF 则直接保留 Inter VF 的轮廓变化。两条上游路径在大量简单字形上已有定点轮廓差异，Italic 复合字形还会受组件位置和 `USE_MY_METRICS` 影响，因此本项目不把 VF 的完整 bbox 强制改成静态成品。VF 的 Inter 轮廓在 public `200..900` 的 7 个设计控制点和 6 个中间探测点按允许整字平移的方式核验；六个发布字重的 horizontal/vertical advance、LSB、TSB 则由 HarfBuzz 对项目 unhinted 静态字体逐码位要求 exact。

VF 的破折号与省略号源审计会先把内存中的上游参考写成 SFNT 并重新读取，再比较轮廓与面积，避免把尚未定点量化的浮点参考和已发布二进制混为一谈。横竖 advance 不依赖这份转换参考，而是由 HarfBuzz 直接读取成品及真实 Source Han/Shanggu VF 的 HVAR/VVAR，并要求整数结果完全相同。Italic 为匹配官方 Sarasa 静态度量产生的整字 LSB 平移会单独记录；平移后的轮廓形状，以及六个发布字重对本项目静态字体的逐码位横竖 side bearing，仍是硬性失败项。

## v1.0.40.3 与上游基线的差异

`v1.0.40.3` 以 Sarasa Gothic `v1.0.40` 为上游基线；最后的 `.3` 是本仓库自己的衍生版本号，不是上游 Sarasa Gothic 的官方小版本。本版保留 `v1.0.40.2` 恢复的 Noto CJK GPOS `chws` / `vchw`，并完成字重体系、命名、法律元数据和审计覆盖修正：

- `chws` 用于横排连续全角标点的上下文压缩，`vchw` 用于对应的竖排压缩；单独包围正文的全角标点不会被无条件压缩。
- Source Han Sans `2.005R` 底稿和 Sarasa Gothic `1.0.40` 参考字体本身不含这两个 FeatureRecord。Noto CJK 与 Source Han Sans 共用 CJK 字形源，但在官方交付构建中另用 `add-chws` 后处理加入这两个特性；本项目跟随的是这条 Noto CJK 交付路径。
- 后处理固定使用 [`chws_tool 1.4.5`](https://github.com/googlefonts/chws_tool) 和 `east-asian-spacing 1.4.5`，并在所有轮廓、hint、metrics、GSUB 和 Sarasa GPOS 基础模板处理完成后执行。
- 相对同一字体的 `v1.0.40` 成品，这一步只新增 GPOS FeatureRecord 与 contextual positioning lookup，并重算 `head.checkSumAdjustment`；不会修改 cmap、glyf/gvar 轮廓、TrueType instructions、hmtx/vmtx advance、GSUB、GDEF 或公开字重轴。
- Italic VF 的 CJK 斜体仍由对应地区的正体 CJK VF 以 `9.4°` 仿斜生成。构建会先在原始正体坐标空间展开 `gvar` 中全部 IUP 隐含增量，再同时剪切基础轮廓与显式增量；最后四个 metric phantom points 不参与剪切。这样不会让 IUP 在剪切后的坐标系中重新解释稀疏增量，修复了旧成品在非默认字重下随机出现的 CJK 笔画过粗或过细。
- VF 的 nameID 25 使用只含 ASCII 字母数字的 Variations PostScript Name Prefix；从 Inter 导入 `cv14` 时也会同步或重映射 FeatureParams 引用的界面名称记录，保证所有布局表 name ID 引用都可解析并可完成 TTX roundtrip。
- 公开字重改为 `200/300/400/600/700/900` 六档，移除 `Normal 350` 和 `Medium 500` 命名实例。`SemiBold 600` 沿用 Sarasa 静态配对：CJK 取 Source Han Sans Medium `500`，Latin 取 Inter SemiBold `600`；`350` 只作为隐藏的 `avar` 插值锚点，不再作为公开样式。
- VF 会先把 CJK 底稿切换到 public 轴，再重映射 Inter `gvar` support。构建脚本对此调用顺序设有硬性检查；发布审计会在 public `200/300/350/400/600/700/900` 对照同数字 Inter 轮廓，并在 `250/325/375/500/650/800` 增加中间探测；`wght=600` 另与 Source Han internal `500` 核验 CJK 轮廓及轴坐标，避免只在局部轴段出现的静默错配。
- 静态 TTF 改回上游同样的 `post` format 3，不保存虚构或与 GID 不一致的 glyph names。VF 和静态字体都保留 Sarasa、Inter、Adobe、Google 的版权，CL 另保留 Shanggu Fonts 的原版权声明；nameID 13/14 直接写入 OFL 1.1 说明与链接，单独分发 TTF 也不再只剩 Adobe 元数据。Heavy 的 legacy 样式保持 Regular 位而不冒充 Bold；静态 `ulCodePageRange1` 保留显式 bit 31；VF 通过 MVAR 与静态字重同步下划线粗细和位置。
- hinted 与 unhinted 静态 TTF 的 nameID 3/5 会明确写入互斥的变体标识，避免操作系统把两套文件判断为重复字体。unhinted 的 `maxp.maxZones` 规范为 `1`，`gasp` 末端规范为 `0xFFFF`，不再依赖 OTS 丢弃上游的非规范表。静态最终化只允许 `head/maxp/OS/2/name/gasp/GSUB` 改变；其中 GSUB 只加入省略号的语言与竖排路由。`glyf/loca`、instructions、hmtx/vmtx、GDEF、GPOS 等其余表会在覆盖前后逐表核对 SHA-256，因此不重新 hint。
- 修复 Heavy 后处理把 Bold 参考的 glyf、hmtx/vmtx、VORG、bbox 与 `palt` 覆盖到 900 成品的问题。Heavy 现在只借用 Bold 的公开 cmap、layout、命名与 hint 配置边界；24 个 Heavy 静态字体的实际 glyph 数据来自同次 Source Han/Shanggu Heavy 与 Inter Black pass2。逐码位来源审计比较 1,110,144 个码位，结果为 0 missing、0 outline、0 metric failure。
- 最终 name table 与官方 Sarasa/Source Han 一样不保留 platform 1（Macintosh）记录；156 个字体共删除 732 条由 STAT/fvar 构建器生成的 Mac 记录，Windows/Unicode 本地化名称及上述来源版权不变。构建器、resume 校验和发布审计都把残留 Mac 记录视为失败。
- 破折号改为静态与 VF 共用 Source Han/Shanggu 的 `ccmp → locl → vert/vrt2` 结构。SC/TC/HC/J/K 的非 CJK 默认路径保留比例长字形，CJK 地区路径严格为 `1em/2em/3em`；KOR 单字和 CL 全局映射分别忠实保留两套上游边界。静态逐字重使用对应 Source Han/Shanggu 静态轮廓，VF 使用同一来源的可变轮廓与 advance，而不是从 Regular 冻结或从静态组合字形重新插值。每个 VF 仍以 `0.5` 字重步长遍历 `200..900` 的 1401 个位置，并在七个轴映射控制点逐语义字形核对上游轮廓、HarfBuzz 解析的真实 HVAR/VVAR advance 与墨量单调性；轮廓参考必须先经过 SFNT round-trip，不能用未量化内存数据制造假失败。
- 破折号地区映射优先在 Sarasa Ui 模板已有的 `locl` FeatureRecord 中原位追加；某个地区 LangSys 没有可用 `locl` 时，才为该语言补充一条 Source Han 映射。SC/TC/HC/J/K 分别覆盖 ZHS/ZHT/ZHH/JAN/KOR，不重排其他 feature 的执行顺序；CAT 的空 `locl` 保持为空，不能误用 ROM/MOL 等非空 lookup。
- 修复省略号的中西文语义。Source Han/Shanggu 的默认 U+2026 与 U+22EF 共用居中 `1em` 字形，`pwid` 另有下沉字形；Sarasa Ui 会把 `pwid` 烘焙为默认轮廓。本项目保留下沉形式作为非 CJK 默认，并用 JAN/KOR/ZHH/ZHS/ZHT `locl` 恢复居中全宽形式，`vert/vrt2` 同时覆盖两种入口。静态与 VF 的六个命名字重、隐藏 `350` 和 VF 全轴 `200..900` 都检查英文下沉、CJK 两字宽与竖排等价；三种语义字形还逐字重对照实际 Source Han/Shanggu 来源。
- 修复 VF 构建末段以静态 Sarasa 模板重建 `GDEF` 时丢失 Source Han ItemVariationStore、却留下 `kern` / `palt` / `vpal` GPOS VariationIndex 引用的问题；例如 SC 正体有 1082 个悬空引用。现在模板只同步 GlyphClassDef / MarkGlyphSetsDef 等静态结构，原有 VarStore 与引用关系完整保留；破折号改为纯 GSUB/glyf/gvar 路径，不再向 GPOS/GDEF 追加自定义定位数据。
- 修复 Noto `chws/vchw` 后处理重新序列化 `glyf` 时重算静态 Italic bbox 的问题。坐标和 instructions 虽未改变，约三万个映射码位的 1 unit bbox 偏差仍会改变 TrueType phantom points，并造成大面积 hinted FreeType 位图差异；现在后处理结束后按对应 Sarasa Ui 静态参考恢复可直接对齐字形的 bbox。最终完整栅格审计覆盖 72 个案例、16,659,660 次 FreeType 渲染，全部为 0 差异，不需要重新运行 hint 分析。
- 修复静态 `palt` 定位值按自动 glyph name 同步造成的错配。`post` format 3 的内存自动名不具备跨字体语义，目标与参考字体中同名 `glyphNNNNN` 可能对应不同 GID 和轮廓；现在按展开后的轮廓结构与横竖 metrics 建立语义映射。五个官方同名字重从对应 Sarasa 参考复制，Heavy 从同次 Sarasa pass2 Heavy/Black 来源复制，避免拿 Bold 的定位值覆盖 900 轮廓。
- 修复 48 个静态 Italic TTF 中破折号坐标替换后产生的 OTS 非法 `OVERLAP_SIMPLE` 编码。编码器仍保留首点 bit 6；只有当后续重复 flag 因 x/y 压缩位不同而无法挂入首 flag 的 repeat run 时，才清除那个无语义的显式重复 bit。共规范化 100 个 glyph，修复前后 8 个 ppem、hinted/no-hinting 两种模式的 1,600 次位图比较全部一致，不需要重新 hint。
- VF 最终重建完整的 `HVAR`/`VVAR`，而不是让 HarfBuzz 等引擎只看到默认 `hmtx/vmtx`。七个轴控制点逐 glyph 校准 advance、LSB/RSB、vertical advance、TSB/BSB 和 vertical origin；TrueType VF 的默认轮廓同时满足 `hmtx.lsb == glyf.xMin`，并正确设置 `head.flags` bit 1、清除 bit 5。LSB 校正 variation 只平移真实轮廓点，四个 metric phantom points 保持零增量；否则 phantom 平移会抵消可见轮廓平移，造成 FontTools 表面正常而 HarfBuzz side bearing 仍错误。最终直接由 HarfBuzz 逐码位读取七个控制点，不能只靠实例化结果通过。
- 修复 Italic VF 在非默认字重下复合拉丁字形的组件相对位置漂移。构建现在先物化并重编译全部继承的上游 `gvar`，完成 LSB/xMin 与 HVAR/VVAR 校准后，先直接校正默认 `400` 的基础复合组件，再只为 12 个非默认探测点中实际超出 2 units 的字形追加组件 tuple；简单字形、组件数量或变换不匹配都会硬失败。旧 SC Italic 不仅在 `300/350/600/700` 分别有 `138/139/139/139` 个 mismatch，`200/400/900` 也各有 136 个；新流程的 13 点均为 0。SC 正式构建直接校正 136 个默认复合字形，只需 40 条非默认 tuple、2 条 1-unit 边距回填和 18 条 HarfBuzz 整字平移，最大平移 6 units。随后按组件依赖顺序恢复决定运行时 LSB/TSB 的 `xMin/yMax`，并用只做整字平移的 HarfBuzz 校准消除组件修复带来的定点边距回归；整字平移不改变 Inter 相对轮廓。该修复只重建 6 个 Italic VF，不改静态 TTF，也不重新 hint。
- 默认实例的轮廓平移按 TrueType 组合字形依赖从组件到父字形排序，每处理一个父字形都重新计算 bbox；否则基础字形和父字形会被按旧 bbox 重复平移。`gvar` 的坐标长度始终由最终 `glyf` 结构决定，不能沿用旧 tuple 的点数；写出前会物化并重编译全部 tuple，任何原始越界点号或最终坐标长度不符都会使构建、resume 和发布审计失败。
- 收紧自审计：产品差异不再以 `diagnostic_` 前缀自动降级，CL 也不再排除绝大多数 cmap；原始 `glyf` flag stream 会绕过 FontTools 的展开缓存直接解析。审计门禁使用显式节注册表，任何新节未注册都会硬失败；跳过像素审计的 checkpoint 只能标为 `PARTIAL`，不能显示 `PASSED`。除 FontTools 实例外，另以 HarfBuzz 直接检查 12 个 VF 的六个命名字重和隐藏 `350` 锚点的横竖 advance 与 side bearing。FontTools 在命名字重静态化时产生的 hmtx LSB/vmtx side-bearing 差异只有在同 case 的 HarfBuzz 全码位为 0、没有 advance/缺字等其他失败、普通字形水平不超过 2 units、垂直不超过 1 unit 时才记为观察；带 `USE_MY_METRICS` 的复合字形水平单列并限制在 16 units 内，原始计数、直方图和最大值全部写入报告。
- 最终完整审计状态为 `PASSED`，全部门禁 failure 与 coverage failure 均为 0。静态 raster 共 72 个 case、16,659,660 次 FreeType 渲染比较，mismatch 为 0；VF 的 7 个 Inter 设计点与 6 个中间点分别完成 166,068 和 142,344 次轮廓比较，HarfBuzz 度量覆盖 3,886,848 个码位。独立 OTS `9.3.0` 对 `156/156` 个字体均只输出成功信息；FontBakery `1.1.0` 的正式分地区门共 318 项 PASS，包括 `font_version 156/156`、`no_mac_entries 156/156` 和成对 `STAT/ital_axis 6/6`。完整代表 profile 的上游/通用 CJK 基线与混合集合调用假失败继续原样列入报告，不把原始 FAIL 总数伪装成 0。

## 地区

- `CL`：传统旧字形。静态 TTF 的汉字底稿先取 `SourceHanSansK`，再用 Shanggu Sans `1.028` 官方 `ShangguSansTC` 静态 TTF 覆盖传统旧字形；VF 使用 `SourceHanSansK-VF` 加 `ShangguSansTC-VF` 覆盖。最终公开字符集与 layout 模板以 Sarasa Ui CL 为边界；五个官方同名字重的非数字 metrics 对齐 Sarasa Ui CL，Heavy 保留 Shanggu Heavy 来源数据。
- `SC`：简体中文，来源为 `Source Han Sans SC`。
- `TC`：繁体中文台湾字形，来源为 `Source Han Sans TC`。
- `HC`：繁体中文香港字形，来源为 `Source Han Sans HC`。
- `J`：日文字形，来源为 `Source Han Sans J`。
- `K`：韩文字形，来源为 `Source Han Sans K`。

## 文件结构

```text
fonts/
  variable/
    Sarasa-Ui-VF-PropDigits-SC[wght].ttf
    Sarasa-Ui-VF-PropDigits-SC-Italic[wght].ttf
    Sarasa-Ui-VF-PropDigits-CL[wght].ttf
    Sarasa-Ui-VF-PropDigits-CL-Italic[wght].ttf
    Sarasa-Ui-VF-PropDigits-TC[wght].ttf
    Sarasa-Ui-VF-PropDigits-TC-Italic[wght].ttf
    ...
  static/
    SarasaUiPropDigitsCL-TTF-1.0.40.3/
    SarasaUiPropDigitsCL-TTF-Unhinted-1.0.40.3/
    SarasaUiPropDigitsSC-TTF-1.0.40.3/
    SarasaUiPropDigitsSC-TTF-Unhinted-1.0.40.3/
    ...
reports/
  Sarasa-Ui-PropDigits-report.json
  font-inspection.json
  release-audit.json
tools/
  build_sarasa_ui_propdigits_sc.py
```

## 构建逻辑

VF 不从静态字重插值生成。它直接合并对应地区的 CJK VF 与 Inter VF：

- `CL` 使用 `SourceHanSansK-VF.ttf`，并用 Shanggu Sans `1.028` 的 `ShangguSansTC-VF.ttf` 覆盖传统旧字形；静态 CL 使用同一 release 的 `ShangguSansTC-*.ttf` 作为传统旧字形覆盖源
- `SC` 使用 `SourceHanSansSC-VF.ttf`
- `TC` 使用 `SourceHanSansTC-VF.ttf`
- `HC` 使用 `SourceHanSansHC-VF.ttf`
- `J` 使用 `SourceHanSans-VF.ttf`
- `K` 使用 `SourceHanSansK-VF.ttf`
- Latin 正体使用 `InterVariable.ttf`，Italic 使用 `InterVariable-Italic.woff2` 或同名 TTF

构建时对齐 Sarasa Ui 的处理方式：

- VF 的公开 `wght` 轴是 `200..900`，默认值 `400`。Source Han Sans VF 内部仍按 `250/400/900` 裁剪并参与插值；最终 `avar` 使用分段映射：public `200/300/350/400/600/700/900` 分别对应 Source Han internal `250/300/350/400/500/700/900`。Inter VF 直接按 public `200/400/900` 裁剪，Inter 的轮廓变化始终钉在同数字公开坐标，所以 public `600` 是 Source Han Medium `500` 与 Inter SemiBold `600`，而不是两边都取 Medium `500`。
- CJK Italic VF 在合并 Inter Italic 之前由 CJK 正体 VF 仿斜生成。`gvar` tuple 的 IUP 隐含增量必须先按未剪切轮廓坐标完整展开，再做仿射剪切；直接剪切稀疏 delta 会改变后续 IUP 插值结果，虽然默认实例可能正常，其他轴位置却会出现局部笔画墨量突变。构建只变换真实轮廓点，不把四个 hmtx/vmtx phantom point 的垂直 delta 混入水平 metrics。
- Inter 先烘焙 Sarasa 原版给 Inter 配置的 `ss03` 和 `cv10`。
- 码位归属遵循 Sarasa pass1 的优先级，并按 VF 源文件实际覆盖做兜底：Latin 和西文符号优先来自 Inter VF；CJK、Hangul、Jamo 和 Sarasa Ui 的本地化标点优先来自对应地区的 Source Han Sans VF。
- Source Han 侧先烘焙 Ui 标点需要的 `pwid` 替换，并执行 Sarasa 式符号清洗，例如 `·`、弯引号、短横、省略号和注音扩展符号宽度处理。U+2026 的下沉 `pwid` 形式保留为非 CJK 默认；最终 GSUB 再通过地区 `locl` 恢复 Source Han 默认的居中全宽形式。U+2E3A/U+2E3B 不再进入 Sarasa 的 `stretchDual/stretchTri` 清洗，而是连同 U+2014/U+2015 直接保留 Source Han/Shanggu 的地区、字重和可变度量语义。
- Hangul / Jamo 宽度归一到全角。
- 最终 GSUB 保留上游 Sarasa Ui 有的 `ccmp`，并保留裁剪到上游覆盖范围的 `locl`、Hangul Jamo、`vert` `vrt2`、`tnum` `pnum`、中文破折号、语言相关省略号、上游暴露的空 `cv01..cv13` / `ss01..ss08` 标签，以及与 Inter 兼容的冒号 `calt`。Italic 按上游口径不暴露 `cv11`。
- 最终静态 `GSUB` 的 FeatureRecord 顺序和 Script / LangSys 覆盖顺序按对应地区、对应样式的上游 Sarasa Ui 静态字体套模板；基础 `GPOS` 也会同步 FeatureRecord lookup index 与 LookupList 的类型、flag、subtable 形状。`palt` 下假名等已有 glyph 的 SinglePos 取值按语义映射：五个官方同名字重同步对应 Sarasa 参考，Heavy 同步同次 pass2 Heavy/Black 来源。VF 套用静态 GDEF class/mark 模板时保留 Source Han GDEF ItemVariationStore，使原有 `kern` / `palt` / `vpal` VariationIndex 继续落在正确的公开字重坐标。Source Han 的静态与 VF 发布物本来就可能给同一 `palt/vpal` 样本不同定位值，因此 VF 忠实保留 Source Han VF 的可变定位数据；两条上游路径的合理差异不会被伪装成数值相同。完成模板同步后，再统一追加来自 Noto CJK 的 `chws` / `vchw` FeatureRecord 与 contextual positioning lookup。
- Static/VF 都在最终模板之后重建同一套上游破折号 GSUB：`ccmp` 处理比例与全宽双/三连，地区 `locl` 处理 SC/TC/HC/J/K 的全宽及 KOR 单字特例，`vert/vrt2` 处理竖排一、二、三字形；CL 使用 Shanggu 的全局映射而不新增地区 `locl`。旧 Sarasa `calt` continuation glyph、pair-start 状态和破折号 GPOS PairPos 必须全部删除。静态轮廓逐字重来自对应 Source Han/Shanggu 静态源；Italic 对这些正体轮廓做同一 `9.4°` 剪切。VF 先把来源 `HVAR/VVAR` 的破折号变化转入 `gvar` phantom points，完成所有轮廓与度量校正后，再从最终七个控制实例重建覆盖整字体的 `HVAR/VVAR`；合并阶段的来源 metric variation 表不会原样沿用，也不会让成品只在 Regular 正常。
- 省略号使用同一批既有 glyph：`Latn/en` 下 U+2026 指向下沉比例形式，`Hani` 下五个 CJK 语言系统的 `locl` 指向 U+22EF 所在的居中全宽形式，所有 `vert/vrt2` FeatureRecord 都把两种横排入口指向同一个竖排形式。连续两个 U+2026 不做 ligature，CJK 下总 advance 必须是 `2em`。这一步只改 GSUB，静态成品的轮廓、hint 和 metrics 均保持原字节。
- 按 [OpenType HVAR 规范](https://learn.microsoft.com/typography/opentype/spec/hvar)，TrueType VF 默认 LSB 必须等于 `glyf.xMin`，并应提供可随机访问的 metric variation。构建会对默认轮廓和其余六个控制点做水平平移校正，在不改变既定 advance/side bearing 的前提下满足 LSB/xMin 约束；随后为 HVAR 写入 advance、LSB、RSB，为 VVAR 写入 advance、TSB、BSB、vertical origin，并校准 F2Dot14 序列化造成的 1 unit 边界。
- public `350` 只是 `avar` 插值锚点，不是发布实例。六个发布字重的 VF 与项目静态字体仍逐码位要求 HarfBuzz 横竖 advance、LSB、TSB 全部 exact；`350` 另外用 FontTools 实例交叉检查。FontTools 与 HarfBuzz 对 `gvar/IUP`、复合字形和仿斜坐标的定点求值会留下水平最多 2 units、垂直最多 5 units 的 side-bearing 差异，该差异作为具名观察项完整计数；missing glyph、任一 advance 差异或任一方向超限仍直接失败。
- VF 的 GPOS lookup 结构不以静态官方 Sarasa Ui 为逐项等同目标，因为 VF 由对应地区 CJK VF 与 Inter VF 合并生成；发布审计改为检查 VF 的压力实例、cmap、hmtx / vmtx、公开字重轴、数字/冒号行为，以及 exact-weight metrics。当前仅发现 `U+00B7` 在 CL / J / K 与 SC 的 side bearing 有地区标点边界差异，不属于广泛 Latin 源漂移。
- VF、hinted 静态 TTF 和 unhinted 静态 TTF 都包含 `STAT`。VF 的 `STAT` 描述 `wght`/`ital` 轴和命名实例；静态 TTF 的 `STAT` 只用于现代应用识别 weight / italic 样式，不表示静态文件仍有 `fvar` `gvar` 可变轴。
- `OS/2.achVendID` 使用本派生项目的 `MRDK`，不继承上游 Sarasa Ui 的 `????` 占位值，也不冒充 Source Han Sans 或 Inter 的官方 vendor。
- VF 的 Variations PostScript Name Prefix（nameID 25）不直接复用带连字符的 nameID 6，而使用 `SarasaUiVFPropDigits{REGION}[Italic]` 形式；导入 Inter `cv14` 的 FeatureParams 时保留 “Alternate capital sharp S” 界面名称记录，避免悬空 name ID。
- `head.fontRevision` 使用 OpenType 16.16 fixed 可表达的项目数值 `1.0403`，对应本仓库版本 `1.0.40.3`；nameID 5 以 `Version 1.0403; project 1.0.40.3; ...` 开头。OpenType 的版本字段是单个 `major.minor` 数值，不能把四段发布版本直接放在首个数字里；完整发布版本因此保留在后续 `project` 字段、nameID 3、目录名和 Release tag 中。
- 五个官方同名字重会按对应地区的上游 Sarasa Ui 同步非数字与非冒号 advance、横向 LSB、垂直指标、`GDEF`、`VORG`、`vmtx`、glyf flags/bbox/组件结构及可安全继承的 `head`/`OS/2` 字段。Heavy 没有 Sarasa 同名静态参考，只借用 Bold 的公开 cmap、layout、命名和 hint 配置边界；其 glyf、hmtx/vmtx、VORG、bbox 与 `palt` 保留实际 Source Han/Shanggu Heavy 和 Inter Black 的 pass2 数据。数字、Inter 冒号上下文、Source Han/Shanggu 破折号结构及 CL 的 Shanggu 来源仍是刻意差异。
- 静态 TTF 不从 VF 实例化。hinted 和 unhinted 两套都使用静态 Source Han Sans 与静态 Inter，按 Sarasa 上游的 `pass1`、`kanji`、`hangul`、`pass2` 片段流程构建；CL 在 `kanji` 阶段额外使用 Shanggu Sans `1.028` 官方静态 TTF 覆盖传统旧字形。最终 TTF 会按对应 Sarasa Ui 参考裁剪 cmap、回补空 `cv/ss` FeatureRecord，并套用 GSUB/GPOS 模板；五个官方同名字重同步 glyph metrics 与结构，Heavy 则保留 Heavy/Black pass2 glyph 数据。SemiBold 直接采用 `Source Han Medium + Inter SemiBold` 配对。随后默认数字和 `:` remap 到已有 pnum glyph，并加入 Inter colon-run `calt`、中文名、metadata 与静态 `STAT`。
- hinted 静态 TTF 会重新 hint 本项目实际生成的片段。每个字重固定建立与 Sarasa 上游相同顺序的完整环境：8 个家族、6 个地区、正斜体共 96 个 `pass1` 输入，再加 6 个 `kanji` 和 6 个 `hangul` 输入。`pass1` 先经过 `ttfautohint`；Chlorophytum 分别生成完整 `pass1` 组和完整 FE 组的高层 hint 数据，最后按 `pass1`、`kanji`、`hangul` 顺序把全部 108 个输入交给一次统一 `instruct`，再由 `pass2` 取出 Ui 成品需要的片段。这个流程逐项对应上游 `GroupHintSelfPass1`、`GroupHintSelfFe`、`GroupInstr`，也保证全量构建、指定地区构建和断点恢复不会因分析输入缩小而产生另一套全局 TrueType 函数。高层 hint store 中的 glyph 项按数字 GID 规范化；`sharedHints` 则严格按 Sarasa hcfg 的 `Ideograph`、`Hiragana`、`Katakana` 语义顺序写入，因为其数组位置会决定最终 TrueType 函数 ID，不能按名称重新排序。ExtraLight、Light、Regular、SemiBold、Bold 使用上游同名 reference/hcfg 环境；只有 Heavy 没有同名 Sarasa 静态成品，其 Ui 与 FE 使用实际 900 轮廓，辅助拉丁环境和 hcfg 采用 Bold 边界，绝不复制相邻成品的 glyph instructions。最终破折号替换只在相同四点矩形拓扑间复制对应字重坐标，并保留该成品已经生成的 glyph program，因此不需要重新运行整组高层 hint 分析；审计会另外要求所有 hinted 破折号角色都有 instructions、hinted/unhinted 轮廓一致，并通过多 ppem FreeType 栅格检查。
- unhinted 静态 TTF 使用相同的静态片段路径，但跳过 `ttfautohint` 和 Chlorophytum，直接由未 hint 的 `pass1`/`kanji`/`hangul` 合成；这是一套正式输出，供需要无 TrueType instructions 版本的使用场景选择。
- 静态 TTF 与上游一样使用 `post` format 3，不保存 glyph names。比例数字与等宽数字的关系由 cmap 和 GSUB 表达；构建不会为了显示名称改变 glyph order，也不会在 subset 前预先制造与最终 GID 不一致的自动名。
- glyph 总数不作为构建目标。脚本会保留和同步 cmap 字形以及 GSUB/GPOS/GDEF 可达的未编码 glyph；不会为了让 `maxp.numGlyphs` 与上游相同而补入不可达 glyph。
- 静态 TTF 不再为了 OTS 清除上游 glyph 首点的 `OVERLAP_SIMPLE`；这个 bit 会影响 FreeType rasterization，exact 样式应与上游保持一致。最终写出会优先把重复 overlap flag 编成 OTS 可接受的首 flag repeat run；坐标替换使 x/y 压缩位不同、无法使用同一 repeat run 时，只清除后续点上无语义且被 OTS 禁止的显式重复 bit 6，首点语义不变。resume 与发布审计会直接解析原始 `glyf/loca` 字节流，fontTools 是否已展开 glyph 都不会让检查失效。unhinted 成品把 `maxp.maxZones` 规范为 `1`，并把 `gasp` 最后范围移到合法的 `0xFFFF` sentinel；这只修正栅格元数据，不加入 instructions，也不改变轮廓。

## 字重

VF 实例和静态 TTF 都使用更接近 Sarasa / CSS 的公开字重体系：

- `ExtraLight`：`200`
- `Light`：`300`
- `Regular`：`400`
- `SemiBold`：`600`
- `Bold`：`700`
- `Heavy`：`900`

`ExtraLight 200` 是 Sarasa/CSS 的公开口径；CJK 轮廓来源仍是 Source Han Sans ExtraLight `250`，不会伪造不存在的 Source Han CJK `200`。`SemiBold 600` 采用 Sarasa 原静态路径的跨上游语义：CJK 是 Source Han Medium `500`，Latin 是 Inter SemiBold `600`。隐藏的 public `350` 仅用于保留连续插值校正点，不出现在 fvar 命名实例、STAT 公开档位或静态文件中。Heavy `900` 是本项目保留的扩展实例，使用 Source Han Heavy `900` 与 Inter Black `900`；两者在极粗端的视觉墨量并不完全相同，这项取舍优先保持各上游的终点语义、轴单调性和未来可重建性。

## 文件

- VF：[fonts/variable](fonts/variable)
- hinted 静态 TTF：[fonts/static](fonts/static)
- unhinted 静态 TTF：[fonts/static](fonts/static)

每个地区、每套静态版包含 12 个文件：6 个字重，每个字重有正体和 Italic。两套静态 TTF 都从 Sarasa 静态片段路径构建；hinted 版额外经过 `ttfautohint` 和 Sarasa 上游 Chlorophytum 的 hint 流程，unhinted 版保留无 TrueType instructions 的静态输出。

发布页会提供按地区拆分和全集打包的 TTF 压缩包：

每个地区有三套包，共 18 个 ZIP：

- `Sarasa-Ui-VF-PropDigits-{REGION}-TTF-1.0.40.3.zip`：单个地区的正体和 Italic 可变 TTF。
- `SarasaUiPropDigits{REGION}-TTF-1.0.40.3.zip`：单个地区的 hinted 静态 TTF。
- `SarasaUiPropDigits{REGION}-TTF-Unhinted-1.0.40.3.zip`：单个地区的 unhinted 静态 TTF。

另有三套全地区合集：

- `Sarasa-Ui-VF-PropDigits-TTF-1.0.40.3.zip`：全部地区的可变 TTF。
- `SarasaUiPropDigits-TTF-1.0.40.3.zip`：全部地区的 hinted 静态 TTF。
- `SarasaUiPropDigits-TTF-Unhinted-1.0.40.3.zip`：全部地区的 unhinted 静态 TTF。

合计 21 个 ZIP。`1.0.40` 表示 Sarasa Gothic 上游基线，最后的 `.3` 表示本仓库衍生发布。

## 构建

构建脚本是：

```powershell
python tools\build_sarasa_ui_propdigits_sc.py
```

如果只重建两套静态 TTF、保留现有 VF 输出，可以用：

```powershell
python tools\build_sarasa_ui_propdigits_sc.py --static-only
```

如果只写出指定地区，可以用：

```powershell
python tools\build_sarasa_ui_propdigits_sc.py --regions SC,TC
```

VF 和 unhinted 静态 TTF 只处理所选地区。hinted 静态 TTF 为了保持上游全局 hint 环境，仍会准备全部六地区的分析输入，只把所选地区写入成品目录。

静态 hinted 构建默认使用本机 CPU 核心数作为 Chlorophytum 并行数。需要限制或指定并行数时：

```powershell
$env:SARASA_HINT_JOBS = "16"
python tools\build_sarasa_ui_propdigits_sc.py
```

构建完整 hint 环境时，外围字体准备默认最多同时执行 4 个任务，以避免 32 GB 内存机器在 Node 字体合并阶段出现过高峰值；Chlorophytum 分析内部仍使用 `SARASA_HINT_JOBS` 指定的核心数。外围并发可单独调整：

```powershell
$env:SARASA_HINT_PREP_JOBS = "4"
```

中断后需要继续静态构建时，可以显式使用 `--resume-static`：

```powershell
python tools\build_sarasa_ui_propdigits_sc.py --static-only --regions SC --resume-static
```

恢复模式不会清空现有静态输出目录，只会跳过已经完整存在的字重；同一字重必须同时存在 hinted 与 unhinted、正体与 Italic 四个文件才会被视为完整。只要某个字重仍需生成，hinted 路径就会恢复该字重固定的六地区完整分析环境。默认构建仍会先清理目标地区的静态 TTF 输出，以保证从零复现。

只需收口现有静态成品的版本、法律元数据、unhinted 栅格元数据或最终 GSUB 路由时，可以使用内建的受保护最终化模式：

```powershell
python tools\build_sarasa_ui_propdigits_sc.py --refresh-static-finalization-only
```

该模式在覆盖前后逐表比较哈希，只允许 `head`、`maxp`、`OS/2`、`name`、`gasp`、`GSUB` 改变；GSUB 仅用于正式构建末段的省略号 `locl/vert/vrt2` 路由。`glyf/loca`、TrueType instructions、hmtx/vmtx、GDEF、GPOS 等其余表有任一字节变化都会拒绝写回，写回前还会重新验证破折号、省略号和 CAT 空 `locl`。这是构建器的最终化入口，不是独立 repair 或兼容脚本，也不会重新 hint。

VF 可用 `--resume-variable` 断点恢复。它不会仅凭文件存在或时间戳跳过，而会先读取成品并检查 public `wght` 轴、六个命名实例、完整 `avar` 锚点与 600→Source Han 500 映射、项目版本、各来源版权、vendor、MVAR、HVAR/VVAR 完整 metric map、默认 LSB/xMin、`head.flags`、`chws/vchw`、破折号与省略号；任一项不符都会重建该正体或 Italic 文件。完整恢复命令为：

```powershell
python tools\build_sarasa_ui_propdigits_sc.py --resume-variable --resume-static
```

脚本会在缺失依赖或源文件时准备固定版本的构建输入：Sarasa Gothic `v1.0.40`、各地区 `SarasaUi{REGION}` TTF `1.0.40` hinted/unhinted、Source Han Sans `2.005R` VF、Shanggu Sans `1.028` 静态 TTF 与 VF、Inter `v4.1`、Node.js `v26.7.0`，以及 Sarasa 上游 npm 依赖。Python 包版本见 [requirements-build.txt](requirements-build.txt) 与 [requirements-audit.txt](requirements-audit.txt)：构建固定 `fontTools 4.63.0`、`uharfbuzz 0.56.0`、`brotli 1.2.0`、`ttfautohint-py 0.6.1`、`py7zr 1.1.3`、`afdko 5.0.1`、`chws_tool 1.4.5`、`east-asian-spacing 1.4.5`，审计另固定 `freetype-py 2.5.1` 与 `numpy 2.4.2`。

若当前 Python 已经完整安装这些精确版本，脚本直接复用，不重复下载；只要缺少或版本不同，就在仓库 `.build-cache/python-venv/` 建立私有虚拟环境、只向其中安装固定依赖并自动重新执行当前命令，不修改系统或用户级 Python。可用 `SARASA_PYTHON_VENV` 指定另一个私有环境；`SARASA_SKIP_PYTHON_DEPS=1` 只适合已经由外部环境严格准备依赖的场景。静态 Source Han TTC 转换会使用 AFDKO 提供的 `otc2otf`/`otf2ttf`；CL 的 Shanggu 覆盖源直接使用 Shanggu 官方发布 TTF，不再通过本地 AFDKO 把 Sarasa 内置旧 subset OTF 转成 TTF。

已有输入会按适用条件核验：Sarasa 参考字体检查 nameID 5 版本，下载或复用其 12 个六地区发布包时同时检查上游 SHA-256；Sarasa 源码检查发布 commit、源码归档 SHA-256、`package.json` 版本与 lockfile 哈希；Source Han Sans VF、Inter VF、Shanggu 发布包和按平台选择的 Node.js 官方归档都检查固定 SHA-256。不一致的输入不会被静默接受。ZIP/7z/tar 在解包前还会验证全部成员路径，拒绝绝对路径、目录穿越与越界链接。

相对 `v1.0.40` 的构建环境，Node.js 从 `26.3.0` 升到 `26.7.0`，`uharfbuzz` 从 `0.55.0` 升到 `0.56.0`，`ttfautohint-py` 从 `0.6.0` 升到 `0.6.1`，`py7zr` 从 `1.1.0` 升到 `1.1.3`，并新增 `chws_tool/east-asian-spacing 1.4.5`。其中 `ttfautohint-py 0.6.1` 只新增 `SOURCE_DATE_EPOCH` 传递和构建兼容修复，没有更换 hint 算法；`py7zr` 只参与上游归档解包；Node 仍执行 Sarasa `1.0.40` 自己的 lockfile；`uharfbuzz 0.56.0` 下生成的 `chws` / `vchw` GPOS 已与旧核心结果逐表对照一致。发布审计还会检查所有成品的轮廓、hint、metrics 和 shaping，工具升级本身不作为新增字体设计差异。

因此，clone 后通常只需要直接运行构建脚本。脚本会把下载缓存放在同级 `source-archives/`，把 Sarasa Gothic 上游源码放在同级 `Sarasa-Gothic-1.0.40/`，把官方 Sarasa Ui 参考字体放在同级 `official-sarasa-ui-1.0.40/`，把 VF 和 Shanggu 静态覆盖输入放在同级 `vf-sources/`，其中 CL 覆盖用的 `ShangguSansTC-VF.ttf` 会放在 `vf-sources/shanggu-1.028/`，`ShangguSansTC-*.ttf` 会放在 `vf-sources/shanggu-1.028/static/`；固定 Node.js 运行时放在同级 `node/`。Sarasa 源码会核验发布 commit、`package.json` 版本和 `package-lock.json` SHA-256；没有 Git 时也按同一固定 commit 下载并校验源码归档。npm 依赖按上游 lockfile 使用 `npm ci` 安装，并以 lockfile 哈希判断能否复用。`npm ci` 会执行该固定依赖树声明的 lifecycle script，这是复现 Sarasa 自身工具链的一部分；不要对不受信任的 fork 直接复用本项目的自动安装假设。默认会使用固定 Node.js，而不是系统里碰巧安装的 Node；只有显式设置 `SARASA_NODE`、`NODE` 或 `NPM` 时才会改用外部运行时。

这些固定版本是本次发布的可复现输入，不是永久锁死的上游边界。未来升级 Sarasa Gothic、Shanggu Sans、Inter 或 Source Han Sans 时，应同步更新脚本中的版本常量、下载校验和与审计基准，并重新生成字体与报告。

可用环境变量覆盖：

- `SARASA_WORK_ROOT`
- `VF_SOURCE_DIR`
- `SOURCE_HAN_SC_VF`
- `SOURCE_HAN_TC_VF`
- `SOURCE_HAN_HC_VF`
- `SOURCE_HAN_J_VF`
- `SOURCE_HAN_K_VF`
- `SHANGGU_CL_VF`
- `CLASSICAL_CL_VF`
- `SHANGGU_SANS_TC_VF`
- `SHANGGU_CLASSICAL_VF`
- `SHANGGU_CL_STATIC_DIR`
- `CLASSICAL_CL_STATIC_DIR`
- `SHANGGU_SANS_TC_STATIC_DIR`
- `SHANGGU_CLASSICAL_STATIC_DIR`
- `INTER_VF`
- `INTER_ITALIC_VF`
- `REFERENCE_SARASA_ROOT`
- `REFERENCE_SARASA_SC_ROOT`
- `REFERENCE_SARASA`
- `REFERENCE_SARASA_HINTED_DIR`
- `TTFAUTOHINT`
- `OTC2OTF`
- `OTF2TTF`
- `SARASA_SOURCE_DIR`
- `SARASA_NODE`
- `SARASA_NODE_DIR`
- `NODE`
- `NPM`
- `SARASA_CHLOROPHYTUM`
- `SARASA_HINT_JOBS`
- `SARASA_HINT_PREP_JOBS`
- `SARASA_BUILD_CACHE`
- `SARASA_DISABLE_BUILD_CACHE`
- `SARASA_PYTHON_VENV`
- `SARASA_SKIP_PYTHON_DEPS`
- `SARASA_SKIP_SOURCE_BOOTSTRAP`

静态 hinted 构建按源文件、上游提交、工具版本和 hint store 顺序规则为每个字重建立持久工作区，并缓存 96 个 `pass1`、12 个 FE 输入及完整分组的高层 hint 数据，位置是 `.build-cache/sarasa-ui-propdigits/`。持久工作区让一次完整分析意外中断后可以直接复用已准备好的 108 个字体；高层 hint 缓存则只在整个字重分析成功后以原子方式写入。缓存不会把部分地区的旧分析结果拼成一个新分组，也不保存已经 `instruct` 的半成品 TTF；命中高层 hint 缓存后仍会把同一字重的全部 108 个输入交给一次统一 `instruct`。

缓存键只包含 hint 阶段的输入内容、上游提交、hcfg、工具版本和显式 `hint_recipe_sha256`，不再包含仓库绝对路径或整个构建脚本/发布版本。修改 README、版权、name table 或其他最终元数据不会无意义地抛弃高层 hint；真正改变片段生成、分析环境或 `sharedHints` 语义顺序时则必须更新 hint recipe，精确失效对应阶段。完成后的静态后处理另有包含项目版本和法律元数据的独立 key。需要冷构建时可设置 `SARASA_DISABLE_BUILD_CACHE=1`，或用 `SARASA_BUILD_CACHE` 指向其他缓存目录。

生成 Release 资产使用仓库内的正式入口：

```powershell
python tools\package_release.py
```

脚本固定 ZIP 成员顺序、`1980-01-01` 时间戳、文件权限和压缩参数，生成六地区各三套包及三个全集包，共 21 个 ZIP；每个包都在根目录包含 `LICENSE.txt`、中文 `NOTICE.md` 和中文 `README.md`。写出后会逐包核验成员顺序、CRC、时间戳、权限、压缩方式与文件大小，并流式比较每个成员和仓库源文件的 SHA-256，再生成 `SHA256SUMS.txt`；已有资产可用 `--verify-only` 复核。

字体检查报告见 [reports/font-inspection.json](reports/font-inspection.json)，构建报告见 [reports/Sarasa-Ui-PropDigits-report.json](reports/Sarasa-Ui-PropDigits-report.json)，发布前 exact、layout/shaping 与像素审计见 [reports/release-audit.json](reports/release-audit.json)。外部工具结果另见 [reports/ots-audit.json](reports/ots-audit.json) 与 [reports/fontbakery-audit.json](reports/fontbakery-audit.json)。正式入口为 `python tools\audit_sarasa_ui_propdigits.py`。layout 模板审计会逐个静态 TTF 比对 GSUB FeatureRecord、FeatureIndex 与 Script/LangSys 执行顺序：SC/TC/HC/J/K 只允许在原 Sarasa Ui 模板中补入 Source Han 必需的 ZHS `locl`，JAN/KOR/ZHH/ZHT 必须原位扩展；CL 不增加破折号地区映射，只允许为缺少 `locl` 的 CJK LangSys 补一条共享的省略号映射。CAT 的 `locl` 必须存在且保持空；基础 GPOS 必须逐 lookup 对齐 Sarasa Ui，随后只允许追加 Noto `chws/vchw` 的 6 个 lookup，任何旧破折号 PairPos 都会失败。

审计报告的 `gate.sections` 是发布结论的唯一来源。每一节必须显式注册并记录 `passed/failed/skipped`；新增结果却忘记接入门禁会让审计器自身失败。`--skip-raster` 生成的非像素 checkpoint 和 `--raster-only` 局部报告只能得到 `complete=false`、`passed=false` 的 `PARTIAL` 状态；只有所有节执行且 failure 总数为 0，最终报告才能标为 `PASSED`。

破折号审计分三层。第一层在 en、zh-Hans、zh-Hant、zh-HK、ja、ko 下分别 shape 单字、双连、三连、U+2E3A/U+2E3B 和横竖排，核对默认比例路径、CJK 严格 `1em/2em/3em`、KOR 单字特例、CL 全局全宽路径以及重复输入与编码长字形的 glyph 等价关系。第二层对 144 个静态 TTF 逐一打开实际 Source Han/Shanggu 静态构建源，比较最多 10 个语义角色的全部轮廓点、hmtx/vmtx、Italic `9.4°` 剪切结果与 instructions，并跨六字重检查面积单调且 Heavy 明显粗于 ExtraLight。第三层为每个 VF 重建一份仅含同一上游轴映射的参考，先将它写入并重新读取 SFNT，再于 public `200/300/350/400/600/700/900` 对照全部角色的轮廓、面积与 side bearing；横竖 advance 则由 HarfBuzz 直接读取成品和真实上游 VF 的 HVAR/VVAR，整数结果必须 exact。随后按 25 一档检查墨量曲线，并以 `0.5` 字重步长遍历 `200..900` 的 1401 个位置，检查默认比例 advance 单调、五种 CJK 语言横竖二字宽和与上游实例的 shaping parity。9/12/16/20/24/48 ppem FreeType 栅格检查覆盖正常单 glyph 路径；仅 `vrt2` 的上游多 glyph 边界允许保持各个相同竖排字形自身的间隙，但 glyph 身份、位图指标和总 advance 必须一致。

省略号另有独立门禁。144 个静态 TTF 会在 en、zh-Hans、zh-Hant、zh-HK、ja、ko 下检查横竖 shaping，并把下沉比例、居中全宽、竖排三种 glyph 的轮廓、四项度量、hint 状态和字重面积曲线逐一对照实际 Source Han/Shanggu 静态源。12 个 VF 在六个命名字重与隐藏 `350` 上做同样的来源及 shaping 对照，并以 `0.5` 步长遍历 `200..900` 的 1401 个位置；每个位置都实际检查 zh-Hans、zh-Hant、zh-HK、ja、ko 五种 LangSys。任一位置出现英文不再低于 CJK、CJK 横排不再是两个居中 glyph/`2em`，或竖排不再是两个竖排 glyph/`2em`，都会使发布失败。VF 源轮廓参考同样必须先经过 SFNT round-trip；真实上游 HVAR/VVAR advance 与成品必须 exact。Upright 的微小定点平移按具名量化项记录；Italic 为对齐官方 Sarasa 静态度量产生的整字 LSB 平移另行公开，但平移后轮廓形状和 VF 对本项目静态成品的六个命名字重 HarfBuzz metrics 仍必须 exact。

其余审计继续覆盖静态 exact 全码位轮廓/原始 flags/metrics 与 hinted FreeType 位图、`palt` 下 `かなカナ`、PropDigits、VF exact metrics、CJK/Inter 源配对、整条中西文字重曲线、CJK Italic 剪切、`chws/vchw`、`kern/palt/vpal` VariationIndex、nameID 引用、版权和版本元数据。VF 度量会先用 FontTools 在六个命名字重实例化比较，再用 HarfBuzz 不实例化地直接读取同六档及隐藏 `350`，逐 cmap 检查横竖 advance、LSB 和 TSB；HVAR/VVAR 的 side-bearing map、默认 LSB/xMin 与 `head.flags` 另作结构门禁。所有报告路径都会在写出前转成仓库相对路径；Windows 盘符以及 macOS/Linux 的 `/Users`、`/home`、`/root`、`/mnt/*` 本机路径都会使发布失败。

外部工具复核使用独立 OTS `9.3.0` 和 FontBakery `1.1.0`。unhinted 成品已把 `maxp.maxZones` 与 `gasp` sentinel 规范为合法值，因此不再把 OTS 丢弃该表当作“继承上游即可”的发布基线；仍会把 OTS 的完整 stdout/stderr 和返回码写入报告。FontBakery 的 `opentype/font_version` 必须对全部项目字体 PASS。完整代表性 profile 并不会显示 0 FAIL：官方 Sarasa 可复现 `base_has_width`、`case_mapping`、9 MiB `file_size` 上限以及 unhinted 的 `smart_dropout`，Source Han VF 还可复现 31 字符家族名限制；这些是上游数据或通用 profile 对完整 CJK/unhinted 字体的边界。单独检查 Italic 时出现的 `STAT/ital_axis` 是找不到 Roman 配对造成的调用假失败，正斜体成对传入后为 PASS。

FontBakery 通用 profile 中可由官方 Sarasa 或 Source Han 复现的 GDEF mark/spacing、`post` format 3、`xAvgCharWidth`、数学符号宽度、空 `ss` 描述和 VF 不可达保留 glyph 等 WARN 仍会原样记录；轮廓数量与重叠线段 WARN 所指 glyph 也必须由静态 exact、Heavy 来源和 VF 来源配对审计覆盖。报告以“项目新增且无法由上游解释的 failure 为 0”为发布门槛，不再把 FontBakery 通用 profile 的原始 FAIL 数量写成 0。

逐点 CJK 审计在上述 `184,302` 个“地区 × 码位”组合的六个命名字重上共比较 `1,105,812` 个轮廓实例。检查要求正斜体 cmap 与轮廓拓扑一致；以全部轮廓点求出最小最大残差的整字平移后，每个 Italic 轮廓点必须落在对应正体 `9.4°` 剪切结果的 `2.0` font units 内。替换前的旧 CL VF 在 `wght=200` 会被该规则检出 `7,968 / 30,717` 个异常、最大残差约 `53.92` units；最终字体为 `0`，六地区最大残差为 `1.692365` units。面积守恒、字重单调性和墨量跨度仍作为独立检查保留。

## 许可证

字体按 SIL Open Font License 1.1 分发，见 [LICENSE](LICENSE)。

这是修改版字体，不是 Sarasa Gothic、Source Han Sans、Inter 或 Shanggu Sans 的官方发布。
