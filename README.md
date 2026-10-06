# 更纱黑体 Ui PropDigits

<p align="center">
  <img
    src="assets/Sarasa_Ui_VF_Specimen.svg"
    alt="Sarasa Ui VF"
    title="Sarasa Ui VF"
  >
</p>

## 为什么要制作它？

曾几何时，我试图使用[更纱黑体（Sarasa Gothic）](https://github.com/be5invis/sarasa-gothic)作为 Android 手机的默认字体（因为个人更喜欢 [SF Pro](https://developer.apple.com/cn/fonts/) / [Inter](https://rsms.me/inter/) 而非 [Roboto](https://fonts.google.com/specimen/Roboto)），但由于修改软件只支持一个字体，我需要一个可变字体。但可惜的是，[原版更纱并不支持](https://github.com/be5invis/Sarasa-Gothic/issues/314)，我只好自己动手，用 Codex 中的 GPT-6 Astra 制作了本字体的可变版本；静态版本以及比例宽数字则是源于我对其成为 [Unigram](https://github.com/unigramdev/unigram) / Windows 默认字体的需要。

而到了 2026 年 8 月，随着上游、各个工具链（如 Node 版本）及我自身需求的更新，我用 GPT-6 Astra 将版本号刷到了 1.0.40（上游更新的是 Mono 版本，与本项目没有直接关系），同时添加了 `chws` / `vchw` 这两个 OpenType 特性，以方便在不能使用 CSS 的 `text-spacing-trim` 的情况下启用标点挤压。

## 简介

Sarasa Ui PropDigits 是面向中西文混排的更纱 Ui 衍生字体。当前版本为 **v1.0.42**，上游同步到 Sarasa Gothic 1.0.42；2026-10-06 修订版同步更新该 tag 的源码和 Release 发布文件。

字体菜单中的家族名缩写为 **Sarasa Ui PD <地区>**，可变系列为 **Sarasa Ui VF PD <地区>**，本地化名称同样使用 PD。字重名称不缩写；文件名和 ZIP 名仍保留 PropDigits。

六地区共 12 个 VF、144 个原生静态 TTF。每地区提供可变、hinted 静态、unhinted 静态三个包，加上三个全地区包，共 21 个 ZIP；不制作 TTC 或 SuperTTC。

| 地区 | CJK 字形来源 | OS/2 码页范围 1 |
| --- | --- | --- |
| CL | Source Han K 基底，Shanggu Sans TC 1.028 的传统旧字形及 CJK 全宽破折号 | `0x8010019F` |
| SC | Source Han Sans SC | `0x8004019F` |
| TC | Source Han Sans TC | `0x8010019F` |
| HC | Source Han Sans HC | `0x8010019F` |
| J | Source Han Sans J | `0x8002019F` |
| K | Source Han Sans K | `0x8028019F` |

CL 的公开 cmap 和 layout 按 Sarasa Ui CL 的地区边界裁剪；西文标点仍使用 Source Han K。静态字体的 `post` 使用 format 3，不人为生成持久化字形名称。家族名、样式、实例名和 PostScript 名一起更新，并检查 Windows 的 31 字符限制。

## 下载与使用

以 SC 为例：

| 包名 | 内容 |
| --- | --- |
| `Sarasa-Ui-VF-PropDigits-SC-TTF-1.0.42.zip` | 正体、斜体两个可变 TTF |
| `SarasaUiPropDigitsSC-TTF-1.0.42.zip` | 六字重正斜体，共 12 个 hinted 静态 TTF |
| `SarasaUiPropDigitsSC-TTF-Unhinted-1.0.42.zip` | 六字重正斜体，共 12 个 unhinted 静态 TTF |

只接受单个字体文件且支持可变 TTF 的工具，可选对应地区的正体 VF；斜体另有文件。实际字重、语言、文字系统和 OpenType 特性由应用选择。字体本身不包含 Android 或 Windows 的系统字体替换工具。

从旧版升级后，请在应用字体菜单重新选择带 `PD` 的家族；文件名沿用 PropDigits，内部家族名已更新。

## 字重

| 公开坐标 | 样式 | Source Han 内部坐标 | Inter 坐标 |
| --- | --- | --- | --- |
| 200 | ExtraLight | 250 | 200 |
| 300 | Light | 300 | 300 |
| 400 | Regular | 400 | 400 |
| 600 | SemiBold | 500 | 600 |
| 700 | Bold | 700 | 700 |
| 900 | Heavy | 900 | 900 |

350 仅为隐藏 `avar` 锚点，CJK 与 Inter 均映射到 350，不提供命名实例。构建先应用 Source Han public axis，再重映射 Inter 的 `gvar` 和 GPOS 变化；顺序不可反转。公开 600 是 Source Han Medium 500 与 Inter SemiBold 600 的组合。

## 数字、标点与语言

以下 **37 个码位**默认使用 Inter 的比例形态，开启 `pnum` 不再改变其默认宽度：

| 类别 | 码位或字符 |
| --- | --- |
| 数字 | `0–9` |
| 空格 | U+0020、U+00A0、U+2008 |
| 标点与括号 | `, . : ; ( ) [ ] { }` |
| 运算符 | `* + < = > ~ ± × ÷ − ≈ ≠ ≤ ≥` |

`tnum` 恢复这些字符的 Inter tabular 形态；数字本身严格等宽。`tnum + zero` 的斜线零与其他数字共享同一 advance 变化曲线，在各字重及各引擎内分别检查，不能用取整误差放行不等宽的数字。

冒号沿用 Inter colon-run `calt`：`1:2`、`1:`、`:2` 上浮；`1:a`、`a:2`、`a:b` 和 `ķļņŗșț` 旁的冒号保持原位；`a::::::::::2` 只有后五个冒号上浮。相同规则覆盖 `tnum`、`pnum`、`zero` 及其组合。非冒号上下文规则和共享 lookup 保留，包括 `->`、`<->` 等 Inter 箭头连字；地区模板必须保留这些规则在默认西文中的引用。CJK 来源的破折号继续使用自己的排版规则。审计对照原始 Inter，检查字符串边界和长度 1 至 16 的连续冒号。

Inter 的 `ss03`、`cv10` 按 Sarasa 的顺序烘焙为 cmap 字形身份，保留替代字形的字偶距、附加符号锚点和组件关系。VF 的可变定位引用与 GDEF 变化存储、mark filtering sets 一起合并。

拉丁、西里尔、希腊文字系统中的西文标点，以固定 Source Han 的实际排版为准，六地区规则一致：

| 语言 | 省略号 `…` | 单破折号 `—` | `——` / `⸺` | `———` / `⸻` |
| --- | --- | --- | --- | --- |
| 不标语言，或 en/de/fr/ru/el 等西文语言 | 下沉、比例宽 | 西文比例形态 | 西文比例形态 | 西文比例形态 |
| zh-Hans / zh-Hant / zh-HK / ja | 居中、全宽 | 全宽 | 2em | 3em |
| ko | 居中、全宽 | 韩文比例形态 | 2em | 3em |

`hani/kana/hang/bopo` 未指定语言时使用所选地区的 CJK 标点；K 默认采用韩文规则，单破折号保留韩文比例形态，显式中文或日文语言按上表切换为全宽。两个 U+2026 严格占 2em。CL 的中文破折号使用 Shanggu 全宽形态。独立标点若没有被应用分配到 CJK 文字系统，会使用西文默认路径；应用传入的 script 与 language 仍然影响结果。原先人为添加的 `ENG ` 例外记录不再需要。

竖排连续破折号使用依次接续的 **1em 片段**，`⸺`、`⸻` 也分别展开为两个、三个片段。每段仍占 1em；上下片段在连接截面相交，纵向没有空隙，后续普通字符也不随斜体连接横向漂移。已有 2em/3em 路径继续要求连接边逐点一致；长串复用原始整数轮廓，斜体在部分字重下允许原轮廓至多 1 unit 的宽度取整差，但连接左边缘必须完全一致且横截面必须重合。横排的 2em/3em 形式保留。分段轮廓在静态 hint 之前生成，pass2 后仅恢复布局引用，禁止重新绘制已 hint 的轮廓。长串通过上下文替换复用已有片段，再以必要的 GPOS `curs` 定位逐段连接；可变连接锚点保留实际 `gvar` 变化，并追加到现有 GDEF 变化存储，原有 Inter 定位数据不变。连接不依赖应用另外开启可选竖排字偶距。主审计覆盖 2–16、32、64、257 段及混合编码，栅格检查另覆盖 4、5、8、16、32 段。最终 `head` 包围盒按全部字形重新计算；Regular 静态与 VF 默认实例的上下界另与固定的同地区 Sarasa Regular 对照，不能继续保留被长竖排字形撑大或已经过期的值。

`chws/vchw` 来自 Noto CJK 交付阶段的 `add-chws` 逻辑，固定 `chws_tool` 与 `east-asian-spacing` 1.4.5。特性存在不等于应用会自动开启标点压缩；HarfBuzz、CoreText、浏览器及具体 Android/Windows 应用需分别验收。

## 度量、轮廓与 hint

静态字体走 Sarasa 的原生 pass1/kanji/hangul/pass2，不由 VF 实例化。完整 hinted 构建按字重分析相同的 96 个 pass1 与 12 个 CJK 输入；局部地区构建也会准备完整 hint 组来源。缺少 Chlorophytum 时直接失败；需要跳过 hint 应使用专门的 unhinted 入口。

Ui pass1 使用 `--fallback-script=none --fallback-scaling`，通过 ttfautohint 原生控制文件将 U+0360、U+22EF 和四个竖排连接片段归入相同 fallback。U+22EF 保留原生输入中的 fallback 行为，避免新增布局改变自动分类；连接片段保留几何边界，避免网格拟合后产生接缝。未分类字形保持原始比例，正常拉丁字母和数字保留 hint。每个 Ui pass1 的全部字形都检查 6–200 px、灰阶/单色/LCD 的严格指令执行；禁止自动 hint 回退。成品审计再检查全部 hinted 字形的 12、16 px，以及回归码位的 6–200 px。Chlorophytum 指令始终按完整的 108 个输入统一生成，不能将单独生成的 Ui 指令与另一组 CJK 指令混用。

hint 缓存绑定实际执行文件的 SHA-256、版本、选项、控制文件、来源及 hint 配置。同一路径下更换外部 ttfautohint 会使相关缓存失效。只修改名称、版权或 GSUB/GPOS 路由，不重新 hint；轮廓、hint 配置、工具或核心静态构建逻辑变化才重建相关部分。

VF 直接合并 CJK VF 与 Inter VF。Italic CJK 先物化 IUP 隐含增量，再剪切 9.4°；最后四个 metric phantom points 不剪切。构建时重建 HVAR/VVAR，最终保留 HVAR，将竖向变化统一到 `gvar` 并移除 VVAR/VORG，避免引擎采用不一致的原点路径。横向原点补偿写入轮廓和组件坐标，保留修改前后的实际运行时边界与度量。

VF 与 unhinted 静态字体使用 `gasp {0xFFFF: 0x000F}` 和开启 smart dropout 的最小 `prep`，同步更新 `maxp`。这段准备程序只控制栅格补点，不含逐字形 hint、`fpgm` 或 `cvt`，不对轮廓进行网格拟合。VF 还删除未被 fvar、STAT 或 FeatureParams 引用的自定义 name 记录。

静态与 VF 分别遵循固定静态源和可变源，来源差异完整记录。发布字重的横竖度量让 HarfBuzz 同时读取两端成品；竖排边界及连接不一致属于失败项。比例标点在 13 个轴控制点对照固定 Source Han 的实际宽度，最终校准只更新 HVAR 的 advance 及对应右边距，保留公开字重与轮廓。FreeType 与 HarfBuzz 的跨引擎检查覆盖 13 个轴点，保留独立整数取整的原始差异；数字等宽不使用容差。Inter 定位独立对照来源，不能把引擎的回退定位当成正确基准。

Windows 的 win 度量仍沿用上游的 968/241。部分字形墨迹超出该范围；例如固定上游 SC Regular 中有 183 个字形超界，其中 144 个可由 cmap 直接访问，GDI 按这组度量裁切时可能缺笔。本版不扩大这组度量。不同应用对行高、裁切和字体边距的使用方式不同，Android 与 Unigram 的实际效果需另行确认。

## 固定来源

2026-10-06 已重新联网检查四个上游，固定版本均未变化：

| 上游 | 本轮查到的最新发布 | 本项目固定来源 |
| --- | --- | --- |
| [Sarasa Gothic](https://github.com/be5invis/Sarasa-Gothic/releases/tag/v1.0.42) | 1.0.42 | 1.0.42，`f31c230af7fb4854f40d96560e2616eafdd85926` |
| [Source Han Sans](https://github.com/adobe-fonts/source-han-sans/releases/tag/2.005R) | 2.005R | 2.005R |
| [Inter](https://github.com/rsms/inter/releases/tag/v4.1) | 4.1 | v4.1 |
| [Shanggu Sans](https://github.com/GuiWonder/Shanggu/releases/tag/1.028) | 1.028 | 1.028 |

Node.js 固定为 26.7.0。下一轮仍需重新联网复核，不能将上述版本视为永久最新。

Sarasa 1.0.42 于北京时间 2026-09-26 22:58 发布。[1.0.41 至 1.0.42 的源码差异](https://github.com/be5invis/Sarasa-Gothic/compare/v1.0.41...v1.0.42)仅涉及版本字段和 Iosevka 系列来源。六地区 60 个 hinted 与 60 个 unhinted 官方 Ui 成品逐表比较后，除 head/name 元数据外全部相同，含轮廓、度量、布局与 hint 表；[完整对照记录](https://github.com/Mr-Drinking/Sarasa-Ui-PropDigits/blob/v1.0.42/reports/upstream-ui-1.0.41-to-1.0.42.json)绑定两份下载归档和全部字体哈希。因此这次上游同步只刷新版本元数据，保留现有轮廓与 hint，再通过新版发布检查。

下载归档、Source Han VF、Inter、Shanggu 与 Node 均核对固定 SHA-256；已解包文件也逐字节对照已验证的归档。Sarasa 优先使用 Git 固定提交，按 Git 对象的原始字节核验全部已跟踪文件，并检查索引及额外文件；源码缓存固定使用 LF，package-lock 的 SHA-256 为 `e6d2198068bdd1f82d4dba37ab4a314046ea25497cf9fa88ff13ecdf90b8e570`。这也修正了此前固定 Windows CRLF 副本导致的跨平台准备失败，依赖版本不变；源码 ZIP 只有另外提供固定哈希后才能作为后备，不接受无验证的归档。

npm 使用已核验的 Node 运行时执行 `npm ci --ignore-scripts`。本次固定依赖没有安装脚本，项目自身脚本仅检查工具是否存在，由正式入口完成相应准备。每次构建重新核对完整 `node_modules` 文件清单与内容哈希，缺失、变更或额外文件都会拒绝复用。

## 构建

```powershell
python tools\build_sarasa_ui_propdigits_sc.py
```

默认来源工作目录为仓库内的 `.build-cache/work`。构建、主审计和视觉入口使用 `.build-cache/build-audit-venv`，包括 `freetype-py==2.5.1`；FontBakery 使用独立的 `.build-cache/fontbakery-venv`，固定 FontBakery 1.1.0 与 freetype-py 2.3.0。两个环境分别执行 `pip check`，不能将不兼容的依赖合并安装。

四份 `requirements-*.lock` 钉住直接及传递依赖的版本和 SHA-256，安装使用 `--require-hashes`；对应 `.txt` 是维护锁文件的输入。环境身份按 venv 目录判断，两个共享基础解释器的环境不会被混淆。模块导入不创建环境、不安装依赖、不重启进程，环境准备仅在显式入口进行。

只构建所选地区 VF，或仅构建 unhinted 静态字体：

```powershell
python tools\build_sarasa_ui_propdigits_sc.py --variable-only --regions SC
python tools\build_sarasa_ui_propdigits_sc.py --unhinted-only --regions SC,TC
```

VF 入口先检查原生 unhinted 度量参考的版本、哈希、样式与布局；缺失或无效时重建所选地区参考，全程跳过 hint。其他入口包括 `--static-only`、`--resume-static`、`--resume-variable` 和 `--force-static-weights`。增量文件必须具有与当前代码和实际工具相符的校验记录，不能仅凭文件存在跳过构建。静态中间字体和最终化缓存同时绑定输入、配方、实际工具及输出 SHA-256；缓存内容损坏、缺少记录或来源改变时重新生成，不能把已有未知字节补写成新校验记录。`--unhinted-only` 和 VF 参考准备也会写入 README 与固定许可证副本。

仅刷新项目版本时可用 `--refresh-version-from <原项目版本>`。入口要求原有整组 156 个字体与完整 OTS 输入记录匹配，在暂存目录更新 nameID 3、5 和 head 版本，逐表证明其他数据未改变；原版本记录与保护表哈希一并保留。它不重新 hint，也不能代替新版的完整发布审计。

构建先写入暂存目录，整组完成并验证后再替换对应成品；失败保留候选文件，既有成品不被提前删除。替换中途发生错误时回滚已完成的替换，包括字体、文档、报告和校验记录。静态与 VF 的最终化入口也使用整组事务；字体暂存与仓库在同一文件系统，外置 `SARASA_BUILD_CACHE` 的校验记录在其自身文件系统暂存，因此不会因路径越界或跨卷 rename 失败而留下混合状态。静态最终化入口 `--refresh-static-finalization-only` 保护真实轮廓、度量和 hint，仅修改白名单内的元数据及路由。VF 最终化入口 `--refresh-variable-finalization-only` 还会校准数字等宽、跨引擎度量表示及四个竖排片段的原生静态控制点，保护原有非连接定位规则及其他非白名单表，并检查 13 个轴点的运行时结果。连续竖排连接只新增必要的 GSUB/GPOS 路由与变化锚点。

仅更新连续竖排布局可使用 `--refresh-layout-only`。该入口整组处理所选地区的静态和 VF，要求来源字体匹配既有哈希记录，只修改 GSUB、GPOS、GDEF 和必要的字体总校验和；逐表证明轮廓、hint、度量及其余 head 字段未变，并验证长串运行时后提交。它不重新 hint，也不能代替发布审计。

网络较慢时使用系统代理：

```powershell
$env:HTTP_PROXY = 'http://127.0.0.1:7897'
$env:HTTPS_PROXY = 'http://127.0.0.1:7897'
```

下载有连接/读取超时、重试、长度与哈希检查，验证成功后才提交缓存文件。不要强行终止仍在工作的长任务。常用路径覆盖项为 `SARASA_WORK_ROOT`、`SARASA_SOURCE_DIR`、`VF_SOURCE_DIR`、`REFERENCE_SARASA_ROOT`、`SARASA_NODE_DIR`、`SARASA_BUILD_CACHE`、`SARASA_PYTHON_VENV` 和 `SARASA_FONTBAKERY_VENV`。

## 发布验收

```powershell
python -m unittest discover -s tools/tests
python tools\audit_sarasa_ui_propdigits.py --raster-jobs 8 --checkpoint-dir .build-cache/release-audit-checkpoints
python tools\check_external_release.py
python tools\render_visual_checks.py
```

主审计有显式 **23 节 gate registry**。未注册、跳过、覆盖不完整均不能得到 PASSED；完整发布要求 `total_failures=0`。schema 2 发布门从实际明细重新计算每节应有的地区、字重、样式和格式用例矩阵，拒绝空章节、缺项、重复或身份错误的记录，同时核验完整码位、轴点、语言、栅格和实际执行的排版用例。打包器独立重算同一套门槛，不信任报告里的总通过标志或汇总计数。保留全码位 metrics、轮廓与 raster、GSUB/GPOS、地区边界、Inter 可变定位、两端真实 HarfBuzz、跨引擎和视觉检查。`--skip-raster`、`--raster-only` 只能生成 PARTIAL。

`--checkpoint-dir` 在每个章节完整返回后原子保存结果。续跑时核对成品字体、固定来源、原生片段、许可证、实际工具、调用参数和审计代码的哈希；损坏或不完整的记录重新计算。已保存的失败项仍然是失败，最终还要重新检查来源和成品未被改动，并通过同一套 23 节覆盖与失败门槛。中间文件不能作为发布报告使用。

版权检查直接读取固定 Source Han 来源中的声明，同时核验根 LICENSE、NOTICE 和 12 份静态目录中的许可证副本。打包前重新比对这 14 份文档的哈希，审计后的修改会使报告失效。

持续集成运行单元测试与 `tools/check_quick_audit.py`，快速检查字体清单、名称、包围盒、数字特性、冒号和竖排分段。快速报告明确标记为非完整发布审计，不能用于放行 Release。

hint 栅格参照从固定上游构造：分别对原始 pass1 独立运行默认及本项目声明的 ttfautohint 策略，再根据实际指令变化确定 fallback；不依赖单一字号的调试文本分类。先证明原生 pass1 与上游字形前缀一致，再应用这一来源规则并保留上游 Chlorophytum 的替换指令。参照不读取待测成品的 hint，不按成品差异选择例外，不新增跳过码位或位图容差；原始上游像素差异、来源与工具哈希一并保留。

全宽省略号的指令要求也从原始 pass1 按原生策略独立计算，记录源文件、控制文件、实际工具及生成参照的哈希。不能假定所有 hinted 字形都必须有指令：原生策略要求 fallback 时必须无字形指令，其余 hinted 标点仍检查指令存在；两种方向的错误都会失败。本轮修正静态标点来源判定及对应的覆盖汇总时，[源码等价验证记录](https://github.com/Mr-Drinking/Sarasa-Ui-PropDigits/blob/v1.0.42/reports/audit-checkpoint-code-transition.json)证明其余源码节点与全部输入未变，保留未变的原始测量结果，重新计算受影响的标点来源章节及覆盖判定；最终由主入口重新汇总 23 节发布门。

OTS 9.3.0 必须 **156/156** 成功，无意外输出或丢表。FontBakery 1.1.0 必须按六地区、每批 **26 字体、`-J 4`** 运行，禁止一次载入 156 字体。发布门共 **630 PASS**：逐字体版本、Mac 名称、家族名长度和 smart dropout 各 156 项，正斜体 STAT 配对 6 项。

上游已知 FAIL 的批准清单在发布工具中显式维护，当前为空。任何新增豁免必须记录检查标识、适用字体、固定来源的同项复现、原因和证据，不能将本项目已发现的缺陷改成观察项或隐藏 FAIL。

视觉生成器为六地区各生成静态、可变、放大细节三张样张，共 **18 张**。静态样张覆盖全部 72 个 hinted 字体的 9、12、16 px。候选报告始终标记为未审阅；生成图片不等于完成验收。中文标签默认使用本项目 SC Regular unhinted，也可指定 `--label-font` 或 `SARASA_VISUAL_LABEL_FONT`，不依赖写死的系统路径。

每张细节图另含七种文字系统／语言组合、37 个字符的默认／pnum／tnum 对照，以及正斜体、静态／VF 的 2em、3em、4 段、16 段与混合编码竖排对照。打包门要求这些用例及对应字体记录完整，不能只保留旧图的总通过标记。

本轮 Android 与 Unigram 实测均按用户决定暂缓，当前状态均为“未实测”。Android 可按[仓库中的验收清单](https://github.com/Mr-Drinking/Sarasa-Ui-PropDigits/blob/v1.0.42/docs/Android验收.md)后续补测；字体审计和样张检查不代替上述目标应用的实测结论。

逐图实际查看后，记录字重递进、正斜体、字偶距、附加符号、数字与冒号组合、语言路由、横竖标点和裁切情况。发布门核验图片与生成器哈希、每张图的审阅状态及具体记录、六地区和 156 字体清单；总记录单独标为通过不能放行。Android 与 Unigram 实机验收另记设备/应用版本、字体哈希、轴点、实际结果和截图，未实测的平台不写成已通过。

发布验收报告绑定同一组字体哈希和对应工具代码，使用可移植路径。Git 原样保存 JSON 报告的字节，避免换行转换改变发布清单中记录的 SHA-256。通过后才打包：

```powershell
python tools\package_release.py
python tools\package_release.py --verify-only
```

打包器要求字体完整、发布审计完整且哈希匹配。每个静态目录仅允许 12 个确定名称的 TTF、README.txt 和 LICENSE-Sarasa-Gothic.txt；额外文件、临时文件、嵌套文件或链接均拒绝入包。默认输出到 `dist/v1.0.42`；21 个 ZIP 全部核验字体与文档内容，并自动生成 `SHA256SUMS.txt` 和 `reports/release-packages.json`。
