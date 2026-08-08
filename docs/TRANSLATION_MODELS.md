# 日中小说 / 漫画翻译模型

## 推荐顺序

1. **通用且容易部署：`qwen3.5:2b`**
   - 直接执行 `ollama pull qwen3.5:2b`。
   - 项目会附加 ACGN 口语化提示、前文译文和低温度参数。
   - 适合作为默认方案，中文自然度明显优于旧的短句 Marian 模型。

2. **ACGN 专用：Murasaki 8B 系列**
   - 面向轻小说、Galgame 和漫画文本。
   - 优先选择 GGUF 量化版本，并通过 `deploy/ollama/Modelfile.murasaki.example` 导入。
   - 下载前确认模型许可证；部分版本限制商业使用。

3. **ACGN 专用：Sakura / OpenSakura 系列**
   - 适合日中轻小说、漫画和游戏文本。
   - 不同版本的基础模型、显存要求和许可证不同，应选择带 GGUF 或 Ollama 部署说明的版本。

## 导入自定义 GGUF

1. 下载模型文件，例如保存到 `D:\models\murasaki.gguf`。
2. 修改 `deploy/ollama/Modelfile.murasaki.example` 的 `FROM` 路径。
3. 执行：

```powershell
ollama create murasaki -f .\deploy\ollama\Modelfile.murasaki.example
ollama list
```

4. 修改 `.local/config.json`：

```json
{
  "translation": {
    "provider": "ollama",
    "ollama_model": "murasaki",
    "style_profile": "acgn_colloquial",
    "temperature": 0.25,
    "context_chars": 1800,
    "context_segments": 4
  }
}
```

5. 重启 `start.ps1`。自定义模型已经出现在 `ollama list` 时，初始化程序不会再次执行远程拉取。

## 口语化策略

程序会同时使用以下措施，而不是只依赖模型名称：

- 将前几段译文作为上下文，帮助判断省略主语、称呼和人物关系。
- 对话要求自然中文口语，旁白保持小说感。
- 禁止逐字保留日语句式、日式中文、文言化和多余连接词。
- 清理模型的 `<think>` 推理块、`译文：` 前缀和多余外层引号。
- 使用较低温度，减少擅自改剧情和术语漂移。
