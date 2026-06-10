# 推送说明

目标仓库：

```text
Mr-Drinking/Sarasa-Ui-PropDigits-SC
```

仓库描述：

```text
Sarasa Ui SC 派生字体，包含默认变宽数字的可变 VF 与静态 TTF 两个系列。
```

建议主题：

```text
font, typeface, sarasa-gothic, source-han-sans, inter, variable-font, proportional-digits, cjk, sc
```

本目录就是准备推送到 GitHub 的仓库内容。原始 `SarasaUiProDigitsSC-TTF-1.0.39.zip` 约 124 MB，不放入普通 Git 仓库；其解包后的 10 个静态 TTF 已放入 `fonts/static/`。GitHub 普通仓库会阻止超过 100 MiB 的单文件。

如需手动重推：

```powershell
cd C:\Users\ShenZehou\Documents\Codex\2026-06-09\goal-sarasa-gothic-ui\outputs\github-publish
git remote set-url origin https://github.com/Mr-Drinking/Sarasa-Ui-PropDigits-SC.git
git push -u origin main
```
