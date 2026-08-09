# GitHub 推广执行指南

本文档是"提高项目知名度"的行动清单，按执行顺序排列。带 ⚠️ 的项需要在 GitHub 网页端手动操作。

## 1. 仓库设置（网页端，10 分钟）

⚠️ 在仓库 **Settings → General** 底部：

- **Topics**（复制粘贴，逗号分隔）：
  `manga-translation, japanese-translation, ocr, local-ai, ollama, llm, comic, vision-language-model, light-novel, chinese-translation, inpainting`
- **Website**：如暂无官网，可留空或填发布帖链接
- **Social preview**：上传 `docs/assets/social-preview.png`（已生成）
- 勾选 Releases 展示区（默认开启）

⚠️ 替换 `README.md` 徽章中的 `YOUR_GITHUB_USERNAME/aoba-translator` 为真实仓库地址（2 处）。

⚠️ 在 **Issues → Labels** 中创建 `good first issue` 标签，并给 1-2 个简单任务打上（例如"Linux 启动脚本 start.sh"、"Dockerfile"）。

## 2. 首屏对比图（发布前必须）

英文 README 首屏预留了 `docs/assets/before-after.png` 占位（当前被注释）。

- 选一页代表性漫画，左右拼接"原文 | 译文"，宽约 1200px
- 注意版权：建议使用自有版权/已授权素材，或明确标注示例来源
- 放好后去掉 README.md 中的 HTML 注释包裹

## 3. Release 发布（网页端）

⚠️ 推送代码后到 **Releases → Draft a new release**，tag 用 `v0.3.0-glm-ocr`，可直接粘贴下方草稿（按需删减）：

```markdown
## v0.3.0 — Vision-model OCR / 视觉模型识别

### Highlights / 亮点
- **glm-ocr replaces manga-ocr** for manga text recognition: CRAFT detection +
  local vision-model reading now handles decorative and outlined fonts that the
  previous OCR garbled. / 漫画识别从 manga-ocr 全量切换为 glm-ocr：
  CRAFT 检测 + 本地视觉模型识别，艺术字、描边字不再乱码。
- **Qwen 3.5 translation with ACGN colloquial prompting** and rolling story context. /
  Qwen 3.5 翻译，ACGN 口语化提示 + 前文上下文。
- Windows one-click setup (`start.ps1`) with Ollama auto-detection and model auto-pull.
  / Windows 一键启动，自动检测安装 Ollama 并拉取模型。

### Breaking / 变更
- `manga-ocr` dependency removed; vision recognition now goes through Ollama
  (`ollama pull glm-ocr` runs automatically on first startup).
  / 移除 manga-ocr 依赖，识别改走 Ollama（首次启动自动拉取 glm-ocr）。

### Known limits / 已知限制
- Vision OCR on CPU is slow (tens of seconds per text box). / CPU 推理较慢。
- Telea inpainting may leave traces on complex artwork. / 复杂画稿修复仍可能有痕迹。
```

## 4. 发布渠道与文案角度

按转化潜力排序：

| 渠道 | 文案角度 | 注意 |
| --- | --- | --- |
| Reddit `r/LocalLLaMA` | "Fully local manga/novel translator — CRAFT + glm-ocr + Qwen, no cloud" | 英文、附对比图、说明可跑 CPU |
| Reddit `r/manga` / `r/MangaTranslators` | 实操效果展示 | 先读版规，部分版面禁工具推广 |
| Hacker News (Show HN) | 技术栈拆解：检测/识别/修复/回嵌四阶段 | 标题克制，正文放架构链接 |
| B 站 / 知乎 | "我用本地 AI 十分钟汉化了一本漫画" | 视频/图文演示流程 |
| V2EX（分享创造节点） | Windows 一键启动、局域网手机可用 | 社区反感纯广告，突出技术细节 |
| Twitter/X | before/after 图 + #LocalLLaMA | 带仓库链接 |

## 5. Awesome 列表 PR

搜索并提交 PR（注意各清单的贡献规则）：

- `awesome-local-ai`
- `awesome-ocr`
- `awesome-ollama`
- `awesome-japanese`（日语学习工具类）

## 6. 后续里程碑（提升 star 转化率）

- [ ] Dockerfile + docker-compose（国际用户最大的门槛）
- [ ] `start.sh` Linux/macOS 启动脚本
- [ ] CONTRIBUTING.md + 开发环境说明
- [ ] GitHub Actions 徽章接入（`.github/workflows/ci.yml` 已就绪，推送即生效）
