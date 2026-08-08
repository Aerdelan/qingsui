from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path

from .translation import Translator


JAPANESE_PATTERN = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?])")


def decode_text(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"

    for encoding in ("utf-8", "cp932", "shift_jis", "utf-16"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8-replace"


def split_long_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    sentences = [item for item in SENTENCE_BOUNDARY.split(text) if item]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(sentence[index : index + max_chars] for index in range(0, len(sentence), max_chars))
            continue
        if current and len(current) + len(sentence) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        chunks.append(current)
    return chunks or [text]


def build_translation_units(text: str, max_chars: int = 420) -> tuple[list[str], list[tuple[str, int | str]]]:
    parts = text.splitlines(keepends=True)
    units: list[str] = []
    layout: list[tuple[str, int | str]] = []
    for part in parts:
        content = part.rstrip("\r\n")
        ending = part[len(content) :]
        if content.strip() and JAPANESE_PATTERN.search(content):
            indexes: list[int] = []
            for chunk in split_long_text(content, max_chars):
                indexes.append(len(units))
                units.append(chunk)
            layout.extend(("unit", index) for index in indexes)
        else:
            layout.append(("literal", content))
        if ending:
            layout.append(("literal", ending))
    if not parts and text:
        layout.append(("literal", text))
    return units, layout


def restore_document(translations: Sequence[str], layout: Sequence[tuple[str, int | str]]) -> str:
    result: list[str] = []
    for kind, value in layout:
        if kind == "unit":
            result.append(translations[int(value)])
        else:
            result.append(str(value))
    return "".join(result)


def translate_novel(
    source: Path,
    destination: Path,
    translator: Translator,
    *,
    max_chars: int = 420,
    batch_size: int = 8,
    context_segments: int = 4,
    context_chars: int = 1800,
    progress: Callable[[int, str], None] | None = None,
) -> dict[str, int | str]:
    text, encoding = decode_text(source.read_bytes())
    units, layout = build_translation_units(text, max_chars=max_chars)
    translated: list[str] = []
    if not units:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
        return {"segments": 0, "source_encoding": encoding, "characters": len(text)}

    context_window: list[str] = []
    for start in range(0, len(units), batch_size):
        batch = units[start : start + batch_size]
        context = "\n".join(context_window)
        context = context[-context_chars:] if context_chars > 0 else ""
        batch_translations = translator.translate_batch(batch, context=context)
        translated.extend(batch_translations)
        if context_segments > 0:
            context_window = (context_window + batch_translations)[-context_segments:]
        if progress:
            completed = min(start + len(batch), len(units))
            progress(completed * 100 // len(units), f"正在翻译文本 {completed}/{len(units)}")

    output = restore_document(translated, layout)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"\xef\xbb\xbf" + output.encode("utf-8"))
    return {"segments": len(units), "source_encoding": encoding, "characters": len(text)}



