from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from .ocr import OcrEngine, merge_regions
from .rendering import process_image_render
from .translation import Translator


ProgressCallback = Callable[[int, str], None]


def translate_manga_images(
    images: Sequence[tuple[Path, Path]],
    ocr_engine: OcrEngine,
    translator: Translator,
    rendering_config: dict,
    *,
    batch_size: int = 8,
    context_chars: int = 1800,
    progress: ProgressCallback | None = None,
) -> list[dict]:
    report: list[dict] = []
    total = max(1, len(images))
    for index, (source, destination) in enumerate(images, start=1):
        base = (index - 1) / total
        if progress:
            progress(int((base + 0.05 / total) * 100), f"OCR：{source.name}")
        regions = merge_regions(ocr_engine.recognize(source))
        page_context = ""
        for start in range(0, len(regions), batch_size):
            batch = regions[start : start + batch_size]
            translations = translator.translate_batch(
                [item.text for item in batch], context=page_context
            )
            for region, translated in zip(batch, translations, strict=True):
                region.translated_text = translated
            updated_context = page_context + "\n" + "\n".join(translations)
            page_context = updated_context[-context_chars:] if context_chars > 0 else ""
        if progress:
            progress(int((base + 0.65 / total) * 100), f"修复背景并回嵌：{source.name}")
        if regions:
            process_image_render(source, destination, regions, rendering_config)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
        report.append(
            {
                "file": source.name,
                "regions": len(regions),
                "average_confidence": round(
                    sum(item.confidence for item in regions) / len(regions), 3
                )
                if regions
                else None,
            }
        )
        if progress:
            progress(index * 100 // total, f"完成图片 {index}/{len(images)}")
    return report


