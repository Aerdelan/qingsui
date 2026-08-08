# 架构说明

## 分层

- `config.py`：首次运行配置、目录和运行状态。
- `environment.py`：操作系统、Python、内存、GPU、外部命令与依赖检测。
- `models.py`：Hugging Face / Ollama 与 EasyOCR 模型拉取。
- `archive.py`：安全解压、输入分类和最终 ZIP 打包。
- `novel.py`：编码检测、日文分段和文档结构还原。
- `ocr.py`：OCR 提供器适配。
- `translation.py`：Transformers、Ollama 和测试用 Echo 翻译器。
- `rendering.py`：文字颜色估计、字形掩码、背景修复和排版回嵌。
- `manga.py`：OCR 区域合并和逐页处理。
- `pipeline.py`：小说 / 漫画统一流水线。
- `jobs.py`：单工作线程后台队列、进度和错误日志。
- `assistant.py`：只读汇总环境、配置、文档和失败日志，并调用本机 Ollama 进行项目诊断。
- `server.py`：标准库 HTTP 服务、助手 API 与静态文件托管。

## 可替换接口

`Translator` 只要求实现：

```python
class Translator(Protocol):
    name: str
    def translate_batch(self, texts: Sequence[str]) -> list[str]: ...
```

OCR 引擎只需返回 `list[TextRegion]`。因此可继续添加 PaddleOCR、manga-ocr、ONNX Runtime 或远程兼容接口，而不改变任务和打包层。

## 并发策略

默认任务池 `max_workers=1`，原因是 OCR 和翻译模型通常会占用大量显存 / 内存，串行执行比同时加载多个模型更稳定。HTTP 上传和状态查询仍由 `ThreadingHTTPServer` 并发处理。

## 安全边界

- Web 服务默认监听 `0.0.0.0:8765` 以支持可信局域网访问；当前没有登录鉴权，不应直接暴露到公网。
- 上传需要明确 `Content-Length` 并受大小限制。
- 压缩包成员会在写入前解析并拒绝绝对路径和 `..`。
- 解压文件数与总尺寸均受配置限制。
- 最终文件名经过清理，工作区以随机任务 ID 隔离。

## 项目诊断助手

`ProjectAssistant` 每次请求都会重新读取环境、模型状态、脱敏配置、最近任务和受限范围内的失败日志，再将这些内容与项目说明一起作为系统上下文传给本机 Ollama。浏览器不能提交自定义 `system` 消息，历史记录只接受 `user` 和 `assistant` 角色，并受消息数量、单条长度和请求体大小限制。

助手是只读诊断层：它不会调用 Shell、不会自动安装依赖、不会修改配置或源码。这样既能让模型了解页面刚出现的错误，也不会把聊天功能变成远程命令执行入口。


