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

Sarasa Ui PropDigits 是面向中西文混排的更纱 Ui 衍生字体。当前版本为 **v1.0.43**，上游同步到 Sarasa Gothic 1.0.43。

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

CL 的公开 cmap 和 layout 按 Sarasa Ui CL 的地区边界裁剪；西文标点仍使用 Source Han K。Sarasa 只覆盖其自带的尚古 1.025 子集（约 7,000 字），本项目使用完整的尚古 1.028，因此每个字重约有 720 个汉字的轮廓与 Sarasa Ui CL 不同（如 㓱 㙦 䌙，上游此处仍为 Source Han K 字形）。这些字保持尚古的原始位置，与 Sarasa 处理其子集内尚古字形的方式相同；官方成品的边距与竖排原点只用于与其字形相同的字。静态字体的 `post` 使用 format 3，不人为生成持久化字形名称。家族名、样式、实例名和 PostScript 名一起更新，并检查 Windows 的 31 字符限制。

## 下载与使用

以 SC 为例：

| 包名 | 内容 |
| --- | --- |
| `Sarasa-Ui-VF-PropDigits-SC-TTF-1.0.43.zip` | 正体、斜体两个可变 TTF |
| `SarasaUiPropDigitsSC-TTF-1.0.43.zip` | 六字重正斜体，共 12 个 hinted 静态 TTF |
| `SarasaUiPropDigitsSC-TTF-Unhinted-1.0.43.zip` | 六字重正斜体，共 12 个 unhinted 静态 TTF |

只接受单个字体文件且支持可变 TTF 的工具，可选对应地区的正体 VF；斜体另有文件。实际字重、语言、文字系统和 OpenType 特性由应用选择。字体本身不包含 Android 或 Windows 的系统字体替换工具。

从旧版升级后，请在应用字体菜单重新选择带 `PD` 的家族。

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

`tnum` 恢复这些字符的 Inter tabular 形态；数字本身严格等宽。`tnum + zero` 的斜线零与其他数字共享同一 advance 变化曲线。

冒号沿用 Inter colon-run `calt`：`1:2`、`1:`、`:2` 上浮；`1:a`、`a:2`、`a:b` 和 `ķļņŗșț` 旁的冒号保持原位；`a::::::::::2` 只有后五个冒号上浮。相同规则覆盖 `tnum`、`pnum`、`zero` 及其组合。非冒号上下文规则和共享 lookup 保留，包括 `->`、`<->` 等 Inter 箭头连字；地区模板必须保留这些规则在默认西文中的引用。CJK 来源的破折号继续使用自己的排版规则。

Inter 先烘焙 `tnum`，再依次烘焙 `ss03`、`cv10`，最后对上表 37 个码位恢复 `pnum`。这一顺序与 Sarasa 原生 pass1 衔接，逗号、分号保留默认外形及 `numr/dnom` 替换。烘焙改变 cmap 字形身份，同时保留替代字形的字偶距、附加符号锚点和组件关系。

VF 按文字系统、语言和特性标签合并 Source Han 与 Inter 的布局引用，同名 `kern/ccmp` 保留双方适用的 lookup，`locl` 不按特性记录序号配对。Source Han 的地区 VF 按语言拆分部分竖排替换（如韩文 VF 的 JAN/ZHH/ZHT 不替换 `！：；？`，日文冒号改用旋转形）；原生静态字体对各语言使用同一组 `vert/vrt2` 时，VF 的这些语言也共用同一路由。路由可取自其他文字系统（TC 的 DFLT 本无 `！：；？` 的竖排替换），也可合并两条来源路由（Sarasa J 旋转冒号，同时替换 `！；？`）；结果按编码或绘制外框逐码位与原生静态字体核对。GDEF 沿用实际来源字形的类别及变化存储，不把静态字体的位置性字形名当作 VF 的身份。静态 GPOS SinglePos 格式 2 保留逐字形取值。GPOS PairSet 按第二字形编号排序：固定上游中部分 PairSet 未排序，HarfBuzz 会漏掉其中已存储的字偶距（如 ExtraLight、Light 的 `とっ`），本项目统一按 OpenType 规范排序。拆分 cmap 别名时同步继承布局输入，CL 的统一码与兼容码先按尚古来源字形分离，再覆盖轮廓。CL 的 `㪉㪘㪣㪰` 明确归为间距汉字，修正上游 GDEF 误标记造成的零步进。

拉丁、西里尔、希腊文字系统中的西文标点，以固定 Source Han 的实际排版为准，六地区规则一致：

| 语言 | 省略号 `…` | 单破折号 `—` | `——` / `⸺` | `———` / `⸻` |
| --- | --- | --- | --- | --- |
| 不标语言，或 en/de/fr/ru/el 等西文语言 | 下沉、比例宽 | 西文比例形态 | 西文比例形态 | 西文比例形态 |
| zh-Hans / zh-Hant / zh-HK / ja | 居中、全宽 | 全宽 | 2em | 3em |
| ko | 居中、全宽 | 韩文比例形态 | 2em | 3em |

`hani/kana/hang/bopo` 未指定语言时使用所选地区的 CJK 标点；K 默认采用韩文规则，单破折号保留韩文比例形态，显式中文或日文语言按上表切换为全宽。两个 U+2026 严格占 2em。独立标点若没有被应用分配到 CJK 文字系统，会使用西文默认路径；应用传入的 script 与 language 仍然影响结果。原先人为添加的 `ENG ` 例外记录不再需要。

竖排连续破折号使用依次接续的 **1em 片段**，`⸺`、`⸻` 也分别展开为两个、三个片段。每段仍占 1em；上下片段在连接截面相交，纵向没有空隙，后续普通字符也不随斜体连接横向漂移。已有 2em/3em 路径继续要求连接边逐点一致；长串复用原始整数轮廓，斜体在部分字重下允许原轮廓至多 1 unit 的宽度取整差，但连接左边缘必须完全一致且横截面必须重合。横排的 2em/3em 形式保留。分段轮廓在静态 hint 之前生成，pass2 后仅恢复布局引用，禁止重新绘制已 hint 的轮廓。长串通过上下文替换复用已有片段，再以必要的 GPOS `curs` 定位逐段连接；可变连接锚点保留实际 `gvar` 变化，并追加到现有 GDEF 变化存储，原有 Inter 定位数据不变。连接不依赖应用另外开启可选竖排字偶距。主审计在七种文字系统、八种语言和三种竖排特性组合下覆盖 2–16、32、64、257 段及混合编码。最终 `head` 包围盒按全部字形重新计算；Regular 静态与 VF 默认实例的上下界另与固定的同地区 Sarasa Regular 对照，不能继续保留被长竖排字形撑大或已经过期的值。

`chws/vchw` 来自 Noto CJK 交付阶段的 `add-chws` 逻辑，固定 `chws_tool` 与 `east-asian-spacing` 1.4.5。它按标点墨迹落在全角框的哪一半决定可挤压的空白；斜体按剪切前的正体形状判断，否则较粗字重的 SC `？` 顶部剪切后越过中线，不再与相邻标点挤压。特性存在不等于应用会自动开启标点压缩；HarfBuzz、CoreText、浏览器及具体 Android/Windows 应用需分别验收。

## 度量、轮廓与 hint

静态字体走 Sarasa 的原生 pass1/kanji/hangul/pass2，不由 VF 实例化。完整 hinted 构建按字重分析相同的 96 个 pass1 与 12 个 CJK 输入；局部地区构建也会准备完整 hint 组来源。缺少 Chlorophytum 时直接失败；需要跳过 hint 应使用专门的 unhinted 入口。

Ui pass1 使用 `--fallback-script=none --fallback-scaling`，通过 ttfautohint 原生控制文件将 U+0360、U+22EF 和四个竖排连接片段归入相同 fallback。U+22EF 保留原生输入中的 fallback 行为，避免新增布局改变自动分类；连接片段保留几何边界，避免网格拟合后产生接缝。未分类字形保持原始比例，正常拉丁字母和数字保留 hint。每个 Ui pass1 的全部字形都检查 6–200 px、灰阶/单色/LCD 的严格指令执行；禁止自动 hint 回退。Chlorophytum 指令始终按完整的 108 个输入统一生成，不能将单独生成的 Ui 指令与另一组 CJK 指令混用。

hint 缓存绑定实际执行文件的 SHA-256、版本、选项、控制文件、来源及 hint 配置。同一路径下更换外部 ttfautohint 会使相关缓存失效。只修改名称、版权或 GSUB/GPOS 路由，不重新 hint；轮廓、hint 配置、工具或核心静态构建逻辑变化才重建相关部分。

VF 直接合并 CJK VF 与 Inter VF。Italic CJK 先物化 IUP 隐含增量，再以 em/3 为剪切中心倾斜 9.4°；此规则覆盖非 cmap 的竖排替代字形，最后四个 metric phantom points 不剪切。剪切后的增量按变化区间的先后逐级取整：每个变化区间在峰值处吸收默认轮廓与先前区间留下的取整误差，因此各峰值处的轮廓都只比实例化后正体轮廓的精确剪切值差 0.5 unit 以内；各自取整时误差会在区间重叠处叠加（`夜` 在 200 差约 2 unit）。静态破折号也使用同一中心；修正这一轮廓时需要重建对应 hint。韩文字形宽度归一化覆盖 GSUB 可达的非编码字形，原本零步进的组合字母仍保留零步进。

构建时重建 HVAR/VVAR，最终保留 HVAR，将竖向变化统一到 `gvar` 并移除 VVAR/VORG，避免引擎采用不一致的原点路径。横向原点补偿写入轮廓和组件坐标。Inter 的轮廓、GPOS 变化和步进曲线保留双方轴映射及来源变化区间的全部折点；控制值直接取自原始 2048 单位的 Inter 曲线，增量在分层模型内逐级取整；轮廓再在 13 个控制字重逐点校正，每次校正只作用到相邻控制字重为止，每个控制点的轮廓误差不超过 0.5 unit。HVAR 步进同样取自原始 Inter，并保留六个原生静态字重的控制值，不用实例化、缩放后已取整的副本重新推算。删除线粗细通过 MVAR `strs` 随字重变化，Heavy 使用自己的删除线粗细与 PANOSE 字重。

VF 与 unhinted 静态字体使用 `gasp {0xFFFF: 0x000F}` 和开启 smart dropout 的最小 `prep`，同步更新 `maxp`。这段准备程序只控制栅格补点，不含逐字形 hint、`fpgm` 或 `cvt`，不对轮廓进行网格拟合。VF 还删除未被 fvar、STAT 或 FeatureParams 引用的自定义 name 记录。

静态与 VF 分别遵循固定静态源和可变源，来源差异完整记录。发布字重的横竖度量让 HarfBuzz 同时读取两端成品；竖排边界及连接不一致属于失败项。横向位置与竖排原点都以实际墨迹为准：VF 与静态字体常以不同的离线控制点画出同一墨迹，例如斜体字形、Inter 的斜体来源与 `Ƴ`、`Ƙ` 的钩，以及 Source Han 可变与静态来源各自转换出的 `〱〲〳`。控制点 xMin（LSB）相等，或控制点 yMax 加 TSB（TrueType 的竖排原点）相等，都会移动墨迹，因此 LSB 与 TSB 取使墨迹与原生静态字体重合的值；步进仍逐字形严格相等。原生静态字体的墨迹按实际绘制的位置计算：上游 Sarasa 每个斜体约两万个字形的 LSB 与字形头比实际轮廓小 1 unit。Inter 组合字形在每个控制字重按原始 Inter 曲线的未取整位置校正组件偏移：各子字形的墨迹中心放到 Inter 中对应子字形的位置，子字形各自对齐时的取整不再累积到整字（此前 `½` 在 700 比静态字体窄约 3 unit），整字保持度量对齐后的墨迹边界。Inter 的可变附加符号锚点与字偶距在每个取样字重只取整一次：缩放到 1000 单位时舍去的默认值小数与原始曲线的变化量合并后取整，再以相邻控制点为界的变化行逐点重现。按 Inter 原有变化区间分别取整时，发布字重处会叠加相邻区间的小数（此前 700 的 `ToWa` 中 o–W 的字偶距为 −49.5，排版取整后比静态字体宽 1 unit）。字形墨迹因对齐原生静态字体而移动时，只有 1 unit 以上的平移才带动所附锚点，并由对应的原始锚点只取整一次；1 unit 以内的移动是原生静态字体自身的取整，其锚点并不随之移动，跟随它会让 600 的 `ê̌`、`Ê̌` 与 SemiBold 静态字体相差 2 unit 以上。比例标点在 13 个轴控制点对照固定 Source Han 的实际宽度，最终校准只更新 HVAR 的 advance 及对应右边距，保留公开字重与轮廓。

Windows 的 win 度量仍沿用上游的 968/241。部分字形墨迹超出该范围；例如固定上游 SC Regular 中有 183 个字形超界，其中 144 个可由 cmap 直接访问，GDI 按这组度量裁切时可能缺笔。本版不扩大这组度量；各应用对行高、裁切和字体边距的处理不同，实际效果以目标应用实测为准。

## 固定来源

2026-10-26 已重新联网检查四个上游，Sarasa Gothic 更新到 1.0.43，其余固定版本未变化：

| 上游 | 本轮查到的最新发布 | 本项目固定来源 |
| --- | --- | --- |
| [Sarasa Gothic](https://github.com/be5invis/Sarasa-Gothic/releases/tag/v1.0.43) | 1.0.43 | 1.0.43，`61e74fda253a1bec0c6f5b09d32a0944bb96b814` |
| [Source Han Sans](https://github.com/adobe-fonts/source-han-sans/releases/tag/2.005R) | 2.005R | 2.005R |
| [Inter](https://github.com/rsms/inter/releases/tag/v4.1) | 4.1 | v4.1 |
| [Shanggu Sans](https://github.com/GuiWonder/Shanggu/releases/tag/1.028) | 1.028 | 1.028 |

Node.js 固定为 26.12.1，Python 为 3.13.17（3.13 系列最后一个提供 Windows 安装包的版本；3.13.18 只发布源码）。下一轮仍需重新联网复核，不能将上述版本视为永久最新。

Sarasa 1.0.43 于北京时间 2026-10-25 09:48 发布。[1.0.42 至 1.0.43 的源码差异](https://github.com/be5invis/Sarasa-Gothic/compare/v1.0.42...v1.0.43)涉及版本字段、Iosevka 系列来源、npm 依赖（ot-builder 1.8.1、piscina 5.3.2 等）以及只影响 TTC 的生成顺序。六地区 60 个 hinted 与 60 个 unhinted 官方 Ui 成品逐表比较后，除 head/name 元数据外全部相同，含轮廓、度量、布局与 hint 表；[完整对照记录](https://github.com/Mr-Drinking/Sarasa-Ui-PropDigits/blob/v1.0.43/reports/upstream-ui-1.0.42-to-1.0.43.json)绑定两份下载归档和全部字体哈希。本版同时升级构建依赖（fontTools 4.67.0、uharfbuzz 0.57.0、Node.js 等），npm 依赖与 fontTools 的变化都会改变 hint 缓存的工具身份或输入指纹，因此全部字体按新工具链重新构建并重新 hint。

下载归档、Source Han VF、Inter、Shanggu 与 Node 均核对固定 SHA-256；已解包文件也逐字节对照已验证的归档。Sarasa 优先使用 Git 固定提交，按 Git 对象的原始字节核验全部已跟踪文件，并检查索引及额外文件；源码缓存固定使用 LF，package-lock 的 SHA-256 为 `616537089de133ea22f98176666408edfdc98fcca2b8babd03d775f799481658`；源码 ZIP 只有另外提供固定哈希后才能作为后备，不接受无验证的归档。

npm 使用已核验的 Node 运行时执行 `npm ci --ignore-scripts`。本次固定依赖没有安装脚本，项目自身脚本仅检查工具是否存在，由正式入口完成相应准备。每次构建重新核对完整 `node_modules` 文件清单与内容哈希，缺失、变更或额外文件都会拒绝复用。

## 构建

```powershell
python tools\build_sarasa_ui_propdigits_sc.py
```

默认来源工作目录为仓库内的 `.build-cache/work`。构建、主审计和视觉入口使用 `.build-cache/build-audit-venv`，包括 `freetype-py==2.5.1`；FontBakery 使用独立的 `.build-cache/fontbakery-venv`，固定 FontBakery 1.1.0 与 freetype-py 2.3.0。两个环境分别执行 `pip check`，不能将不兼容的依赖合并安装。

四份 `requirements-*.lock` 钉住直接及传递依赖的版本和 SHA-256，安装使用 `--require-hashes`；对应 `.txt` 是维护锁文件的输入。已有环境同时核对版本与安装 RECORD 中的文件 SHA-256，损坏时按锁文件重新安装。环境身份按 venv 目录判断，两个共享基础解释器的环境不会被混淆；目标环境重复启动后仍不匹配时明确失败。模块导入不创建环境、不安装依赖、不重启进程，环境准备仅在显式入口进行。`otc2otf` 等工具优先使用明确配置和私有环境中的执行文件。

只构建所选地区 VF，或仅构建 unhinted 静态字体：

```powershell
python tools\build_sarasa_ui_propdigits_sc.py --variable-only --regions SC
python tools\build_sarasa_ui_propdigits_sc.py --unhinted-only --regions SC,TC
```

VF 入口先检查原生 unhinted 度量参考的版本、哈希、样式与布局；缺失或无效时重建所选地区参考，全程跳过 hint。其他入口包括 `--static-only`、`--resume-static`、`--resume-variable` 和 `--force-static-weights`。增量文件必须具有与当前代码和实际工具相符的校验记录，不能仅凭文件存在跳过构建。静态中间字体和最终化缓存同时绑定输入、配方、实际工具及输出 SHA-256；缓存内容损坏、缺少记录或来源改变时重新生成，不能把已有未知字节补写成新校验记录。`--unhinted-only` 和 VF 参考准备也会写入 README 与固定许可证副本。

仅刷新项目版本时可用 `--refresh-version-from <原项目版本>`。入口要求原有整组 156 个字体与完整 OTS 输入记录匹配，在暂存目录更新 nameID 3、5 和 head 版本，逐表证明其他数据未改变；原版本记录与保护表哈希一并保留。

构建先写入暂存目录，整组完成并验证后再替换对应成品；失败保留候选文件，既有成品不被提前删除。替换中途发生错误时回滚已完成的替换，包括字体、文档、报告和校验记录。静态与 VF 的最终化入口也使用整组事务；字体暂存与仓库在同一文件系统，外置 `SARASA_BUILD_CACHE` 的校验记录在其自身文件系统暂存，因此不会因路径越界或跨卷 rename 失败而留下混合状态。静态最终化入口 `--refresh-static-finalization-only` 保护真实轮廓、度量和 hint，仅修改白名单内的元数据及路由。VF 最终化入口 `--refresh-variable-finalization-only` 还会校准数字等宽、跨引擎度量表示及四个竖排片段的原生静态控制点，保护原有非连接定位规则及其他非白名单表，并检查 13 个轴点的运行时结果。连续竖排连接只新增必要的 GSUB/GPOS 路由与变化锚点。

仅更新连续竖排布局可使用 `--refresh-layout-only`。该入口整组处理所选地区的静态和 VF，要求来源字体匹配既有哈希记录，只修改 GSUB、GPOS、GDEF 和必要的字体总校验和；逐表证明轮廓、hint、度量及其余 head 字段未变，并验证长串运行时后提交。以上 `--refresh-*` 入口都不重新 hint，也不能代替完整的发布审计。

网络较慢时使用系统代理：

```powershell
$env:HTTP_PROXY = 'http://127.0.0.1:7897'
$env:HTTPS_PROXY = 'http://127.0.0.1:7897'
```

下载有连接/读取超时、重试、长度与哈希检查，验证成功后才提交缓存文件。不要强行终止仍在工作的长任务。常用路径覆盖项为 `SARASA_WORK_ROOT`、`SARASA_SOURCE_DIR`、`VF_SOURCE_DIR`、`REFERENCE_SARASA_ROOT`、`SARASA_NODE_DIR`、`SARASA_BUILD_CACHE`、`SARASA_PYTHON_VENV` 和 `SARASA_FONTBAKERY_VENV`。

## 发布验收

```powershell
python -m unittest discover -s tools/tests
python tools\audit_sarasa_ui_propdigits.py --jobs 10
python tools\check_external_release.py
python tools\render_visual_checks.py
```

主审计是独立的差分审计，代码位于 `tools/font_audit`，不导入构建模块，也不调用构建脚本自己的判定函数。主干是两条覆盖全面的不变量：发布字重下 VF 与同字重静态字体一致；每个静态字体与固定 Sarasa Ui 同地区成品一致。比较在全 cmap、来源 GSUB 可达的合字与替代序列、多种文字系统与语言、横排竖排，以及 `palt/halt/kern/vpal/vhal/vert/vrt2` 下进行，用 HarfBuzz 实际排版分别比对 cluster、advance、offset、墨迹包围盒和轮廓。横竖名义度量、CJK 字形位置、Inter 对照与附加符号定位按绘制轮廓的实际墨迹测量：HarfBuzz 包围盒包含离线控制点，TrueType 竖排原点随控制点 yMax 变化，且 HarfBuzz 对 2048 单位的缩放字体与 1000 单位成品取整方式不同。步进仍严格相等；名义度量的墨迹位置允许 1 unit 的取整差，排版运行与附加符号的墨迹位置允许 2 unit。附加符号的定位以 1/64 unit 精度读取：HarfBuzz 在 1000 单位下会对 2048 单位来源的锚点默认值与变化量分别取整。VF 的 Inter 轮廓直接对照原始曲线，不对照缩放取整后的副本；CL 汉字在尚古 TC 的可变与静态来源本身不同时（如 㒶），分别对照两份来源；CL 静态字体中与官方成品不同的汉字改为对照固定尚古 1.028 静态来源，必须是相同坐标上的同一轮廓，步进与偏移也完全相同。TrueType 来源与构建一样先剪切存储点，再由其得出隐含的曲线点。差异只有在固定 Inter、Source Han 或尚古来源独立复现时才算有依据；[有意差异清单](tools/font_audit/intentional-differences.json)逐项写明对应的来源核验，清单本身不能跳过比较。Heavy 没有上游成品，因此逐码位对照 Inter Black 与 Source Han Heavy（CL 为尚古 Heavy）的轮廓，并分别比较有、无 GPOS 时的定位效果，不能借用 Bold 作为判定基准。

无法用差分验证的产品规则单独检查：37 个比例码位与 `tnum`、`zero` 的严格等宽（各字重下 FreeType 与 HarfBuzz 各自检查，不设取整容差），冒号上下文对照原始 Inter（VF 对照可变来源，静态字体对照同字重的 Inter 静态来源，含字符串边界和 1–16 个连续冒号），Inter 字偶距与附加符号定位（独立对照来源，不以引擎的回退定位为基准），350 附近的 Inter 步进曲线（200–900 每 5 个单位），竖排连续破折号，`chws/vchw` 在 VF 与静态字体间完全一致且确实生效，13 个轴点的 FreeType/HarfBuzz 跨引擎度量（保留两者各自整数取整的原始差异），复合字形插值，hint 执行与栅格，元数据，地区字符集，尚古覆盖，固定来源与许可证。

主审计共 **23 节**。各节的用例计划从实际字体与来源清单推导，不采信报告自带的计数或完成标志；空章节、缺项、重复、身份错误或用例不足都不能 PASSED，完整发布要求 `total_failures=0`。打包器用当前字体重新推导计划并重算门槛。局部运行（`--regions`、`--sections`）只写入 `.build-cache/release-audit-partial`，不能覆盖 `reports/release-audit.json`。审计按互相独立的单元并行执行；检查点绑定输入字体、审计程序、依赖锁、固定来源与实际工具，任一变化都会重新计算，已保存的失败仍然是失败。

栅格检查：hinted 地区字重对照固定上游成品。参照由原生 pass1 构造：先证明它与上游字形前缀一致，再分别用默认选项和本项目声明的 Ui 策略运行 ttfautohint，由实际指令变化确定 fallback 字形，并保留上游的 Chlorophytum 替换指令。参照不读取待测成品的 hint，不按成品差异选择例外，也不设位图容差。Heavy 与 unhinted 字体要求 hinted/unhinted 两版在不 hint 渲染时逐像素一致。所有 hinted 字形另在 12、16 px 做 FreeType 严格指令执行检查，回归码位覆盖 6–200 px；只有轮廓、字形指令与 CVT 都和同字重固定上游逐字节相同，且上游在同一字号与模式下给出相同错误时，才记为沿用上游，报告保留数量与样本。目前仅 CL Regular 与 Italic 的 龍（U+9F8D）在 57–59 px 超出 FreeType 的循环上限，固定上游 1.0.43 完全相同；非严格渲染与相邻字号一致。其余执行失败一律不放行。

版权检查直接读取固定 Source Han 来源中的声明，同时核验根 LICENSE、NOTICE 和 12 份静态目录中的许可证副本。打包前重新比对这 14 份文档的哈希，审计后的修改会使报告失效。

持续集成先从固定归档准备 Inter 静态/VF、Source Han SC 和六地区 Sarasa 原生参考，再运行单元测试；出现跳过测试即失败。`tools/check_quick_audit.py` 快速检查字体清单、名称、包围盒、数字特性、冒号和竖排分段。快速报告明确标记为非完整发布审计，不能用于放行 Release。

OTS 9.3.0 必须 **156/156** 成功，无意外输出或丢表。FontBakery 1.1.0 必须按六地区、每批 **26 字体、`-J 4`** 运行，禁止一次载入 156 字体。发布门要求总状态通过，且逐项计数共 **630 PASS**：逐字体版本、Mac 名称、家族名长度和 smart dropout 各 156 项，正斜体 STAT 配对 6 项。

上游已知 FAIL 的批准清单在发布工具中显式维护，当前为空。任何新增豁免必须记录检查标识、适用字体、固定来源的同项复现、原因和证据，不能将本项目已发现的缺陷改成观察项或隐藏 FAIL。

视觉生成器为六地区各生成静态、可变、放大细节三张样张，共 **18 张**。静态样张覆盖全部 72 个 hinted 字体的 9、12、16 px。候选报告始终标记为未审阅；生成图片不等于完成验收。中文标签默认使用本项目 SC Regular unhinted，也可指定 `--label-font` 或 `SARASA_VISUAL_LABEL_FONT`，不依赖写死的系统路径。

每张细节图另含七种文字系统／语言组合、37 个字符的默认／pnum／tnum 对照，以及正斜体、静态／VF 的 2em、3em、4 段、16 段与混合编码竖排对照。

逐图实际查看后，记录字重递进、正斜体、字偶距、附加符号、数字与冒号组合、语言路由、横竖标点和裁切情况。发布门核验图片与生成器哈希、每张图的审阅状态及非空的具体记录、上述用例，以及六地区和 156 个字体的清单；只有总记录标为通过不能放行。

本轮 Android 与 Unigram 实测均按用户决定暂缓，当前状态均为“未实测”。Android 可按[仓库中的验收清单](https://github.com/Mr-Drinking/Sarasa-Ui-PropDigits/blob/v1.0.43/docs/Android验收.md)后续补测；实机验收另记设备/应用版本、字体哈希、轴点、实际结果和截图，未实测的平台不写成已通过。字体审计和样张检查不代替目标应用的实测结论。

发布验收报告绑定同一组字体哈希和对应工具代码，使用可移植路径。Git 原样保存 JSON 报告的字节，避免换行转换改变发布清单中记录的 SHA-256。通过后才打包：

```powershell
python tools\package_release.py
python tools\package_release.py --verify-only
```

打包器要求字体完整、发布审计完整且哈希匹配，并重新核对审计绑定的来源和工具身份。每个静态目录仅允许 12 个确定名称的 TTF、README.txt 和 LICENSE-Sarasa-Gothic.txt；额外文件、临时文件、嵌套文件或链接均拒绝入包。默认输出到 `dist/v1.0.43`；新 ZIP 先核验内容再替换旧文件，生成模式写入 `SHA256SUMS.txt` 和 `reports/release-packages.json`。`--verify-only` 只读，不改写这两份文件。
