# 构建与 Hinting 架构

本项目采用 **VF 单真相源** 架构，全部构建、检查、对照、样张与打包逻辑位于 [sarasa_pd/](../sarasa_pd/) 包内，规格集中维护在 [sarasa_pd/spec.toml](../sarasa_pd/spec.toml)。

## 1. 管线总览

```text
Source Han Sans VF 2.005R ─┐
Inter VF 4.1              ─┼─► [Stage VF] 12 个可变字体（6 地区 × 正斜体）
Shanggu Sans TC VF 1.028 ─┘          │
  （仅 CL 叠加）                      ▼
                            [Stage Unhinted] 72 个 unhinted 静态（6 字重实例化）
                                     │
                                     ▼
                            [Stage Hinted] 72 个 hinted 静态（方案 A1）
                                     │
                                     ▼
                       [Check] 规则 / OTS / FreeType 9–128 px / 一致性
                                     │
                                     ▼
                       [Package] 21 个确定性 ZIP + SHA256SUMS + 清单
```

- **VF 阶段**（`sarasa_pd/vf/`）：按显式阶段表执行——加载并子集化固定来源（保留 `cmap` format 14 与 UVS 闭包）→ 限轴至公开 `wght` 空间（CL 核对轴空间后叠加尚古）→ 烘焙 `pwid` 并规整符号与韩文宽度（斜体物化 IUP 后剪切 `9.4°`）→ 缩放并烘焙 Inter（`tnum → ss03 → cv10 → pnum`、扩充 `calt`、重映射 `gvar`/GPOS）→ 精确合并语言系统路由 → 应用 Source Han 原生标点路由（含 `bopo`）→ A5/GDEF/OS/2 修复与不可达字形清理 → `chws_tool` 注入标点挤压 → 校准 `MVAR`/合并同支撑 `gvar` 元组/斜体容差 0 IUP 重优化/由 `gvar` 生成 `HVAR`/写入命名与确定性 `head.modified`。
- **Unhinted 静态阶段**（`sarasa_pd/static/instance.py`）：直接调用 `fontTools.varLib.instancer` 在 6 个命名字重（200/300/400/600/700/900）实例化，清理可变表，设置静态 `OS/2`/命名/`post` format 3 与 smart dropout `gasp`/`prep`。
- **Hinted 静态阶段**（`sarasa_pd/static/hint.py`）：对 unhinted 静态施加方案 A1 混合 hinting（见第 3 节）。

## 2. 固定来源与依赖环境

### 固定上游归档

下载归档均校验固定 SHA-256，解压前严格检查路径穿越与符号链接：

<!-- BEGIN SPEC:sources -->
<!-- END SPEC:sources -->

### 单一 Python 环境

全部构建、检查、对照、样张与打包共用单一虚拟环境（Python 3.13+），由 [requirements.lock](../requirements.lock) 钉住全部直接与传递依赖的版本及哈希，不再需要 `afdko`、`FontBakery` 或 `numpy`：

```powershell
python -m venv .build-cache/venv
.build-cache/venv/Scripts/python.exe -m pip install --require-hashes -r requirements.lock
.build-cache/venv/Scripts/python.exe -E -s -X utf8 -m sarasa_pd env
```

`sarasa_pd env` 校验锁文件哈希完整、已安装包与锁文件一致且无站外同名模块遮蔽。

## 3. 方案 A1：静态 Hinting 与持久缓存

静态 hinted 字体不再走旧版 Sarasa 原生 pass1/kanji/hangul/pass2 管线，而是直接基于 VF 实例化的 unhinted 静态执行两段叠加：

1. **西文与数字（`ttfautohint`）**：使用锁定的 `ttfautohint-py`（或环境变量 `TTFAUTOHINT` 指定的可执行文件），选项 `--fallback-script=none --fallback-scaling`，并通过 `--control-file` 按 GID 将 `U+0360`、`U+22EF` 设为 `none dflt @ <gid>` 保持不缩放；CJK/假名/韩文字形因无西文脚本分类而原样保留未加指令。
2. **CJK / 假名 / 韩文（独立精简 `Chlorophytum`）**：
   - 使用固定 Node.js `v26.12.1` 与 [hint/package.json](../hint/package.json)、[hint/package-lock.json](../hint/package-lock.json) 安装独立精简的 `@chlorophytum/cli` 至 `.build-cache/hint-env`（不依赖整个 Sarasa 源码树）。
   - 6 个字重各有独立配置文件 [hint/hcfg/](../hint/hcfg/)，包含为 900 字重专门调校的 `Heavy.json`（更宽笔画边界与参数，消除粗体汉字与韩文音节在高字号下的笔画畸变）。
   - **跨地区增量持久缓存**：每个字重维护一份按轮廓几何哈希索引的共享缓存 `.build-cache/hint-cache/cache-<Weight>.gz`。同一字重下 6 个地区共享大量相同汉字与假名轮廓；每次 `chlorophytum hint` 在临时副本上运行后再增量合并回共享缓存（避免覆写其他地区的条目），修改字体名或版本号后缓存命中率接近 100%。
   - **斜体零秒复用正体 CJK 指令**：保留 VF 实例化的原始重叠轮廓，使同地区同字重的正体与斜体 CJK/假名/韩文字形点数与轮廓拓扑 100% 对应。重映射阶段强断言全部编码 CJK/假名/韩文轮廓点数与端点完全一致，并按 `#gid`、`cmap`、`cmap` format 14（UVS）与 `(轮廓数, 端点, on-curve, y 坐标序列) + 9.4° 斜切校验` 精确映射全部含未编码 GSUB/UVS 在内的 Chlorophytum 字形，直接交由 `chlorophytum instruct` 注入指令，斜体无需重跑 `chlorophytum hint`。
   - **消除 A13（FreeType 执行预算中止）**：`finalize_hinted_ttf` 将 `cvt ` 表补零扩充至至少 `16384` 项（纯零在 ZIP 中几乎零体积开销，同时把 FreeType 的 DoS 指令步数预算提升至安全裕量以上），使全部字形在 9–128 px 严格扫描下 0 报错且已有字号位图零变化。

## 4. 命令行速查

所有命令均通过 `python -E -s -X utf8 -m sarasa_pd <子命令>` 调用：

| 子命令 | 用途 | 常用参数 |
| --- | --- | --- |
| `env` | 核验 Python 隔离环境与锁文件 | — |
| `build` | 构建字体 | `--stage {vf,unhinted,hinted,all}`、`--regions SC,CL`、`--weights Regular,Heavy`、`--styles upright,italic`、`--subset {full,smoke}`、`--jobs N`、`--out DIR` |
| `check` | 产品规则、OTS、FreeType 9–128 px 与一致性检查 | `--stage {vf,unhinted,hinted,all}`、`--root DIR`、`--regions`、`--weights`、`--jobs N`、`--skip-ots` |
| `compare` | 对比候选字体与上游或前版 | `--baseline {upstream,project}`、`--cand-root DIR`、`--ref-root DIR`、`--ref-version VER`、`--hinted`、`--out DIR` |
| `samples` | 生成 FreeType 6 地区与本机 Windows GDI/DirectWrite 对比样张 | `--cand-root DIR`、`--ref-root DIR`、`--ref-version VER`、`--out DIR`、`--windows-only` |
| `package` | 生成 21 个确定性 ZIP、`SHA256SUMS.txt`、`release-manifest.json` 并只读核验 | `--root DIR`、`--dist DIR` |

网络较慢时可通过标准代理环境变量 `HTTP_PROXY` / `HTTPS_PROXY` 下载上游归档。设置 `SOURCE_DATE_EPOCH` 可覆写构建时间戳。
