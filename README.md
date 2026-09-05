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

Sarasa Ui PropDigits 是面向中西文混排的更纱 Ui 衍生字体。默认 ASCII 数字为比例宽，`tnum` 恢复等宽，`pnum` 恢复比例宽。可变系列提供正体与 Italic，静态系列提供 hinted 与 unhinted。

本轮版本为 **v1.0.40.4**，继续固定 Sarasa Gothic 1.0.40。后缀 `.4` 是本项目的修订号。发布资产包括六地区的 12 个 VF、144 个静态 TTF，以及 21 个 ZIP；不制作 TTC 或 SuperTTC。

| 地区 | 字形来源 |
| --- | --- |
| CL | Source Han K 基底，叠加 Shanggu Sans TC 1.028 的传统旧字形 |
| SC | Source Han Sans SC |
| TC | Source Han Sans TC |
| HC | Source Han Sans HC |
| J | Source Han Sans J |
| K | Source Han Sans K |

CL 的静态与可变字体采用同一 Shanggu 发布版本，公开 cmap 和 layout 按 Sarasa Ui CL 边界裁剪。五个官方同名字重的非数字度量沿用 Sarasa Ui CL，Heavy 使用对应 Heavy 来源。TrueType 成品均移除仅适用于 CFF/CFF2 的 VORG。静态竖排原点由最终 `glyf/vmtx` 决定；VF 通过 `gvar` metric phantom points 保持原点和 advance，复合字形使用独立度量，13 个轴点逐 glyph 核对横向边距与竖排原点。真实轮廓和静态 hint 均保持不变。

## 下载与使用

从对应版本的 Release 下载所需地区和格式。以 SC 为例：

| 包名 | 内容 |
| --- | --- |
| `Sarasa-Ui-VF-PropDigits-SC-TTF-1.0.40.4.zip` | 正体、斜体两个可变 TTF，每个文件含完整公开字重轴 |
| `SarasaUiPropDigitsSC-TTF-1.0.40.4.zip` | 六字重正斜体，共 12 个 hinted 静态 TTF |
| `SarasaUiPropDigitsSC-TTF-Unhinted-1.0.40.4.zip` | 同样 12 个静态样式，不含 hint 指令 |

文件名不含地区的三个包分别汇总全部地区的 VF、hinted 静态或 unhinted 静态字体。只需要一个地区时下载对应地区包即可。只接受单个字体文件且支持可变 TTF 的工具，可使用对应地区的正体 VF；斜体另有独立文件，字重由应用选择。

字体文件提供字形、度量和 OpenType 特性，实际效果还取决于应用是否使用该字体、传入的语言与脚本、选择的字重，以及是否启用相应特性。本项目没有提供 Android 或 Windows 的系统字体替换工具；应用验收范围见下文。

## 字重

| 公开坐标 | 样式 | Source Han 内部坐标 | Inter 坐标 |
| --- | --- | --- | --- |
| 200 | ExtraLight | 250 | 200 |
| 300 | Light | 300 | 300 |
| 400 | Regular | 400 | 400 |
| 600 | SemiBold | 500 | 600 |
| 700 | Bold | 700 | 700 |
| 900 | Heavy | 900 | 900 |

350 仅作为隐藏的 `avar` 锚点，映射到 Source Han 350 和 Inter 350，不提供命名实例。构建必须先应用 Source Han public axis，再重映射 Inter 的轮廓和定位变化。600 的中西文配对是 Source Han Medium 500 与 Inter SemiBold 600。

## 数字、标点与实际排版

- 默认比例数字适合正文和一般界面；需要对齐时开启 `tnum`。冒号采用 Inter 的 colon-run `calt`：`1:2` 上浮，`1:a`、`a:2`、`a:b` 保持原位，连续冒号遵循同一上下文规则。
- Inter 的 `ss03`、`cv10` 按 Sarasa 的顺序通过 cmap 烘焙，保留替代字形自己的字偶距、附加符号锚点和组件身份。可变定位随公开字重变化，Inter 与 CJK 的 GDEF 变化存储及 mark filtering sets 一并合并。
- 六地区未提供语言标记时按各自地区规则，省略号采用居中全宽形式，两个 U+2026 严格占 2em；连续两个、三个破折号分别占 2em、3em。明确的 `Latn/en` 英文路径保留下沉比例省略号和相应地区的比例破折号。CL 的破折号全局沿用 Shanggu 全宽形式。
- 显式 JAN/KOR/ZHH/ZHS/ZHT 本地化与 `vert/vrt2` 继续使用 Source Han/Shanggu 的既有字形。KOR 单破折号保留地区特例，双连与三连仍为 2em、3em。默认语言路由复用已有 lookup，保持 `ccmp → locl → vert/vrt2` 顺序。
- `chws/vchw` 采用 Noto CJK 交付阶段的 `add-chws` 逻辑，固定 `chws_tool` 与 `east-asian-spacing` 1.4.5。是否自动启用取决于排版引擎与应用；支持这两个特性不等于所有 Android 或 Windows 应用默认压缩标点。本轮 Windows Chromium 152 对 `（「更纱」）` 默认已启用压缩：40px 字号下默认与显式开启均为 200px，关闭后为 240px；普通 HarfBuzz 默认调用仍为关闭状态。Android 实机与 Unigram 尚未实测，不能据此推断它们的默认行为。

静态与 VF 各自沿用官方静态源和可变源。来源之间的定点轮廓及整字平移差异会如实记录；发布字重的横竖 advance、LSB、TSB 则让 HarfBuzz 同时读取两端成品进行比较。Inter 定位独立对照原始来源：字偶距比较实际特性调整量，附加符号比较相对基字的真实墨迹位置，避免把 HarfBuzz 的回退定位当成来源基准。附加符号仅测试上游实际支持的堆叠集合，同时核对定位原点与相对墨迹位置；前者按每条附加链接分别允许两次锚点取整累计 2 units，后者包含独立字形边距取整，限为 4 units，原始非零计数和最大值完整保留。

## 本轮修订

- 保留并重映射 Inter GPOS 的变化引用，合并到 GDEF；覆盖字偶距、基字附加符号和堆叠附加符号。
- 修正替代 G 的定位规则、预设组合顺序、mark filtering sets，以及上下文 lookup 的依赖导入和索引映射。
- 修正 CL 静态竖排原点冲突，并增加两端 HarfBuzz、无语言标记横竖排及 Inter 13 个轴点的定位检查。
- 修正局部地区构建的完整 hint 组来源准备；增加完全跳过 hint 的可变及 unhinted 构建入口。
- 补齐审计入口的自动依赖，按虚拟环境目录识别环境，修正 Windows 重启命令时的输出传递。
- 复用解包缓存时逐文件对照已校验归档；固定 Sarasa Git 源码还检查工作树，Node 缓存也核验实际内容。
- 打包必须具有完整字体清单，以及与当前字体 SHA-256 和当前审计代码匹配的主审计、OTS、分地区 FontBakery 和视觉报告。

## 固定来源与联网复核

2026-09-05 重新联网检查了四个上游：

| 上游 | 本轮查到的最新发布 | 本项目固定来源 |
| --- | --- | --- |
| [Sarasa Gothic](https://github.com/be5invis/Sarasa-Gothic/releases/latest) | 1.0.41 | 1.0.40，提交 `4b908c71116a3192f7a9889bd67b1939a891e527` |
| [Source Han Sans](https://github.com/adobe-fonts/source-han-sans/releases/latest) | 2.005R | 2.005R |
| [Inter](https://github.com/rsms/inter/releases/latest) | 4.1 | 4.1 |
| [Shanggu Sans](https://github.com/GuiWonder/Shanggu/releases/latest) | 1.028 | 1.028 |

Sarasa 1.0.41 的 Ui 已有除 head/name 版本信息外等价的核验结论，本轮沿用 1.0.40。下轮仍需重新联网检查，不能将以上版本视为永久最新。

Node.js 固定为 26.7.0。下载归档、Source Han VF、Inter、Shanggu 和 Node 均使用固定 SHA-256；具体校验和保存在构建脚本。已有解包文件必须与已校验归档内容一致，不能只凭路径存在复用。Sarasa 源码检查固定提交、工作树、版本与 lockfile；无 Git 时使用固定 SHA-256 的源码归档。

## 构建

```powershell
python tools\build_sarasa_ui_propdigits_sc.py
```

脚本自动准备固定来源和依赖。已有 Python 环境具有全部精确版本时直接复用；缺少依赖时创建仓库内的 `.build-cache/python-venv`。通过 `SARASA_PYTHON_VENV` 指定私有环境时，始终在该环境执行，即使两个环境共享同一个基础解释器。

只构建所选地区的 VF：

```powershell
python tools\build_sarasa_ui_propdigits_sc.py --variable-only --regions SC
```

缺少度量参考时会先按原生静态片段路径准备所选地区的 unhinted 字体，全程不进入 hint 分析。已有参考则直接使用。

只构建 unhinted 静态字体：

```powershell
python tools\build_sarasa_ui_propdigits_sc.py --unhinted-only --regions SC,TC
```

其他入口包括 `--static-only`、`--resume-static`、`--resume-variable` 和 `--force-static-weights`。最终语言路由或元数据调整可使用 `--refresh-static-finalization-only`、`--refresh-variable-finalization-only`；两者保护真实轮廓与静态 hint；VF 最终化还规范竖排 phantom 数据并核对运行时度量，不重新 hint。

静态字体始终经过 Sarasa 的 pass1/kanji/hangul/pass2，不由 VF 实例化。完整 hinted 构建按字重分析相同的 108 个输入，因此局部地区构建也会准备完整 hint 组来源。只有静态轮廓、hint 配置或核心静态构建逻辑变化才失效对应 hint 缓存。名称、版权、GSUB/GPOS 路由不作为重新 hint 的理由。

VF 直接合并 CJK VF 与 Inter VF，重建 HVAR/VVAR。Italic CJK 先物化 IUP 隐含增量，再以 9.4° 剪切；最后四个 metric phantom points 不剪切。组件插值、整字平移和最终运行时度量分别检查。静态 `post` 保持 format 3。

网络较慢时可设置系统代理：

```powershell
$env:HTTP_PROXY = 'http://127.0.0.1:7897'
$env:HTTPS_PROXY = 'http://127.0.0.1:7897'
```

常用路径覆盖项为 `SARASA_WORK_ROOT`、`SARASA_SOURCE_DIR`、`VF_SOURCE_DIR`、`REFERENCE_SARASA_ROOT`、`SARASA_NODE_DIR`、`SARASA_BUILD_CACHE` 和 `SARASA_PYTHON_VENV`。`SARASA_SKIP_PYTHON_DEPS=1` 与 `SARASA_SKIP_SOURCE_BOOTSTRAP=1` 仅供已由外部流程准备并校验完整输入的环境使用。

## 发布检查与打包

```powershell
python -m unittest discover -s tools/tests
python tools\audit_sarasa_ui_propdigits.py --raster-jobs 8
python tools\check_external_release.py
python tools\render_visual_checks.py
```

主审计保留显式 23 节注册表。未注册、跳过或覆盖不足都不能得到 PASSED；完整发布要求 `total_failures=0`。检查包括全码位轮廓与度量、FreeType 栅格、GSUB/GPOS、地区边界、数字与标点、CJK 剪切、Inter 13 点轮廓及定位、两端 HarfBuzz 运行时、轴与法律元数据。

`--skip-raster` 和 `--raster-only` 只能生成 PARTIAL。完整非栅格检查可通过 `--reuse-non-raster-report` 补跑栅格，但字体清单、SHA-256 和审计代码必须一致。

OTS 9.3.0 必须对 156 个字体全部成功，没有意外输出或丢表。FontBakery 1.1.0 必须按六地区、每批 26 个字体、`-J 4` 运行，发布门要求 318 PASS：逐字体版本与 Mac 名称记录各 156 项，正斜体 STAT 配对 6 项。通用 CJK profile 的上游边界不等于本项目发布门，不能将其原始 FAIL 隐去后声称全部检查为零。

视觉检查单独记录实际字体、轴点、截图和运行环境。主审计、OTS、FontBakery 与视觉报告均绑定同一组 156 个成品的哈希；报告使用可移植路径。

`render_visual_checks.py` 为六地区各生成静态、可变和放大细节样张，共 18 张，保存到 `assets/checks/`；同时生成 `reports/visual-candidates.json`。候选报告始终标记为尚未通过，生成图片本身不等于完成视觉验收。

发布前必须实际查看全部样张，检查字重递进、正斜体、数字与冒号、替代 G 字偶距、堆叠附加符号、横竖标点，以及有无裁切或错位。审阅记录以候选报告的字体和图片哈希为基础，逐图填写 `reviewed`、`passed` 和具体观察，保存为 `reports/visual-audit.json`。只有六地区、全部 156 个字体均已查看且没有未解决问题时，才能将总记录标记为 `complete=true`、`passed=true`。应用实测另记环境、字体哈希、轴点、实际结果和截图；没有实测的平台明确列为未验证。

完成这些检查后再打包：

```powershell
python tools\package_release.py
python tools\package_release.py --verify-only
```

打包器固定生成六地区各三包和三个全地区包，共 21 个 ZIP。每包包含 LICENSE、中文 README/NOTICE，静态包必须恰好有对应 12 个 TTF，地区 VF 包必须有正斜体两个 VF。校验成员顺序、CRC、SHA-256、时间戳、权限和压缩方式，再输出 `SHA256SUMS.txt`。

v1.0.40.4 的已完成结果如下：

| 检查 | 结果与记录 |
| --- | --- |
| 完整主审计 | 23 节全部通过，`total_failures=0`；16,659,660 次 FreeType 栅格渲染，[主审计报告](reports/release-audit.json) |
| OTS | 156/156 通过，无意外输出或丢表，[OTS 报告](reports/ots-audit.json) |
| FontBakery 发布门 | 六地区各 26 字体、`-J 4`，共 318 PASS，[FontBakery 报告](reports/fontbakery-audit.json) |
| 视觉 | 156 字体的 18 张样张及 Windows Chromium 实测截图已查看，[视觉记录](reports/visual-audit.json) |
| 发布包 | 21 个 ZIP 通过生成时校验和独立复验，[包清单与 SHA-256](reports/release-packages.json) |

字体表检查和构建来源另见 [reports](reports) 中的 `font-inspection.json` 与 `Sarasa-Ui-PropDigits-report.json`。上述结果针对本版实际成品；Android 实机与 Unigram 尚未实测，字体审计通过不能替代这些应用的验收。

## 许可证

字体按 SIL Open Font License 1.1 分发，见 [LICENSE](LICENSE) 与 [NOTICE.md](NOTICE.md)。这是修改版字体，不是 Sarasa Gothic、Source Han Sans、Inter 或 Shanggu Sans 的官方发布。
