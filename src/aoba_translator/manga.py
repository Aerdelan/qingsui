from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from .domain import TextRegion
from .ocr import EasyOcrEngine
from .rendering import process_image_render
from .translation import Translator


ProgressCallback = Callable[[int, str], None]


def _overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> int:
    return max(0, min(end_a, end_b) - max(start_a, start_b))


def _should_merge(left: TextRegion, right: TextRegion) -> bool:
    ax0, ay0, ax1, ay1 = left.bounds
    bx0, by0, bx1, by1 = right.bounds
    aw, ah = max(1, ax1 - ax0), max(1, ay1 - ay0)
    bw, bh = max(1, bx1 - bx0), max(1, by1 - by0)
    if left.orientation == right.orientation == "vertical":
        horizontal_gap = max(0, max(ax0, bx0) - min(ax1, bx1))
        vertical_overlap = _overlap(ay0, ay1, by0, by1)
        return horizontal_gap <= max(aw, bw) * 1.2 and vertical_overlap >= min(ah, bh) * 0.2
    if left.orientation == right.orientation == "horizontal":
        vertical_gap = max(0, max(ay0, by0) - min(ay1, by1))
        horizontal_overlap = _overlap(ax0, ax1, bx0, bx1)
        return vertical_gap <= max(ah, bh) * 1.0 and horizontal_overlap >= min(aw, bw) * 0.15
    return False


def merge_regions(regions: Sequence[TextRegion]) -> list[TextRegion]:
    groups: list[list[TextRegion]] = []
    for region in regions:
        matching = [group for group in groups if any(_should_merge(region, item) for item in group)]
        if not matching:
            groups.append([region])
            continue
        primary = matching[0]
        primary.append(region)
        for extra in matching[1:]:
            primary.extend(extra)
            groups.remove(extra)

    merged: list[TextRegion] = []
    for group in groups:
        if len(group) == 1:
            merged.append(group[0])
            continue
        orientation = group[0].orientation
        if orientation == "vertical":
            ordered = sorted(group, key=lambda item: (-item.bounds[0], item.bounds[1]))
        else:
            ordered = sorted(group, key=lambda item: (item.bounds[1], item.bounds[0]))
        x0 = min(item.bounds[0] for item in group)
        y0 = min(item.bounds[1] for item in group)
        x1 = max(item.bounds[2] for item in group)
        y1 = max(item.bounds[3] for item in group)
        confidence = sum(item.confidence for item in group) / len(group)
        merged.append(
            TextRegion(
                polygon=[(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                text="".join(item.text for item in ordered),
                confidence=confidence,
                orientation=orientation,
            )
        )
    return sorted(merged, key=lambda item: (item.bounds[1], item.bounds[0]))


def translate_manga_images(
    images: Sequence[tuple[Path, Path]],
    ocr_engine: EasyOcrEngine,
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


