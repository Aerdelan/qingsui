from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol, Sequence

from .messages import start_command_hint


class TranslationError(RuntimeError):
    pass


class Translator(Protocol):
    name: str

    def translate_batch(
        self, texts: Sequence[str], *, context: str = ""
    ) -> list[str]: ...


class EchoTranslator:
    name = "echo"

    def translate_batch(self, texts: Sequence[str], *, context: str = "") -> list[str]:
        return list(texts)


class OllamaTranslator:
    name = "ollama"

    def __init__(
        self,
        model: str,
        target_language: str = "简体中文",
        *,
        base_url: str = "http://127.0.0.1:11434",
        style_profile: str = "acgn_colloquial",
        temperature: float = 0.25,
        context_chars: int = 1800,
    ) -> None:
        self.model = model
        self.target_language = target_language
        self.endpoint = base_url.rstrip("/") + "/api/chat"
        self.style_profile = style_profile
        self.temperature = max(0.0, min(1.0, temperature))
        self.context_chars = max(0, context_chars)
        self.name = f"ollama:{model}"

    def _system_prompt(self) -> str:
        if self.style_profile == "literal":
            style = "优先准确传达原意，但避免逐字硬译；中文必须自然通顺。"
        else:
            style = (
                "采用自然、口语化、符合中文读者习惯的表达。角色对白像真人说话，"
                "旁白保持小说感但不要翻译腔；禁止日式中文、文言化和不必要的书面连接词。"
            )
        return (
            f"你是专业的日文小说与漫画译者，把日文翻译成{self.target_language}。\n"
            f"{style}\n"
            "翻译规则：\n"
            "1. 结合上下文判断主语、省略成分、人物关系和语气，不要机械直译。\n"
            "2. 对话优先使用中文口语：例如‘んだよ’、‘じゃん’、‘でしょう’要按语境转换，"
            "不要逐字保留日语句式。\n"
            "3. 日语惯用语和四字熟语必须按整体含义意译，严禁逐字硬译："
            "例如‘先頭を切る’是‘带头、率先’，不是‘切开先头’；"
            "‘顔色が悪い’要说‘脸色很差’，不要直说‘贫血’。\n"
            "4. 译文必须符合中文语序和搭配习惯，主语、宾语位置可以调整，"
            "译完后自查一遍，不通顺就重写。\n"
            "5. 保留人名、专有名词、拟声词、情绪、暧昧感和说话人的个性；不要擅自解释。\n"
            "6. 不输出分析、说明、译者注、引号或‘译文：’前缀，只输出译文。\n"
            "7. 输入中的换行要保留；输入只有一段时只输出一段。\n"
            "示例：\n"
            "原文：顔色悪いけど、大丈夫？貧血じゃない？\n"
            "译文：你脸色好差，没事吧？不会是要晕倒了吧？\n"
            "原文：先頭を切って入室すると、そこには五つの椅子が並べられていた。\n"
            "译文：我带头走进房间，只见里面摆着五把椅子。"
        )

    def translate_batch(
        self, texts: Sequence[str], *, context: str = ""
    ) -> list[str]:
        results: list[str] = []
        trimmed_context = context[-self.context_chars :] if self.context_chars else ""
        for text in texts:
            context_block = (
                "前文译文（只用于理解，不要重复输出）：\n"
                + trimmed_context
                + "\n\n"
                if trimmed_context
                else ""
            )
            prompt = (
                context_block
                + "请翻译下面这一段日文，只输出对应的中文译文：\n"
                + text
            )
            payload = json.dumps(
                {
                    "model": self.model,
                    "stream": False,
                    "think": False,
                    "messages": [
                        {"role": "system", "content": self._system_prompt()},
                        {"role": "user", "content": prompt},
                    ],
                    "options": {
                        "temperature": self.temperature,
                        "top_p": 0.85,
                        "repeat_penalty": 1.05,
                        "num_ctx": 4096,
                        "num_predict": 768,
                    },
                },
                ensure_ascii=False,
            ).encode("utf-8")
            request = urllib.request.Request(
                self.endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=600) as response:
                    body = json.loads(response.read().decode("utf-8"))
            except TimeoutError as exc:
                raise TranslationError(
                    "Ollama 推理超时（600 秒）。纯 CPU 推理速度较慢，请减少待译文本量、"
                    "关闭占用内存的程序，或换用更小的模型。"
                ) from exc
            except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                raise TranslationError(
                    "无法连接本机 Ollama 服务，请确认 Ollama 已启动并执行过模型拉取。"
                ) from exc
            if body.get("error"):
                raise TranslationError(f"Ollama 返回错误：{body['error']}")
            message = body.get("message") or {}
            translated = str(message.get("content") or body.get("response") or "").strip()
            if not translated:
                done_reason = str(body.get("done_reason") or "未知")
                raise TranslationError(
                    f"Ollama 返回了空译文（结束原因：{done_reason}）。"
                    "若为 length，说明生成被截断，可尝试换用更小的模型或减少上下文。"
                )
            results.append(clean_translation(translated))
            trimmed_context = (trimmed_context + "\n" + results[-1])[-self.context_chars :]
        return results


class TransformersTranslator:
    name = "transformers"

    def __init__(self, model_path: Path, batch_size: int = 8) -> None:
        try:
            import torch
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except ImportError as exc:
            raise TranslationError(f"翻译依赖未安装。{start_command_hint()}") from exc

        if not model_path.exists():
            raise TranslationError("翻译模型尚未下载，请先在设置页完成初始化。")
        self._torch = torch
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        self._tokenizer = AutoTokenizer.from_pretrained(str(model_path), local_files_only=True)
        self._model = AutoModelForSeq2SeqLM.from_pretrained(
            str(model_path), local_files_only=True
        ).to(self._device)
        self._model.eval()
        self._batch_size = batch_size

    def translate_batch(
        self, texts: Sequence[str], *, context: str = ""
    ) -> list[str]:
        if not texts:
            return []
        encoded = self._tokenizer(
            list(texts),
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=512,
        )
        encoded = {key: value.to(self._device) for key, value in encoded.items()}
        with self._torch.inference_mode():
            generated = self._model.generate(
                **encoded,
                max_new_tokens=512,
                num_beams=4,
                early_stopping=True,
            )
        decoded = self._tokenizer.batch_decode(generated, skip_special_tokens=True)
        return [clean_translation(text) for text in decoded]


def clean_translation(text: str) -> str:
    value = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    value = re.sub(r"^(翻译|译文)\s*[:：]\s*", "", value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'", "“", "”"}:
        value = value[1:-1]
    return value.strip()


def build_translator(config: dict, models_dir: Path) -> Translator:
    provider = str(config.get("provider", "ollama")).lower()
    if provider == "ollama":
        return OllamaTranslator(
            str(config.get("ollama_model", "qwen3.5:2b")),
            str(config.get("target_language", "简体中文")),
            base_url=str(config.get("ollama_base_url", "http://127.0.0.1:11434")),
            style_profile=str(config.get("style_profile", "acgn_colloquial")),
            temperature=float(config.get("temperature", 0.25)),
            context_chars=int(config.get("context_chars", 1800)),
        )
    if provider == "echo":
        return EchoTranslator()
    if provider != "transformers":
        raise TranslationError(f"未知翻译提供器：{provider}")
    return TransformersTranslator(
        models_dir / "translation",
        batch_size=int(config.get("batch_size", 8)),
    )
