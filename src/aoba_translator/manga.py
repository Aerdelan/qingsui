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


def _char_size(region: TextRegion) -> int:
    """估算单个文字块的字符尺寸：横排取行高，竖排取列宽。"""
    x0, y0, x1, y1 = region.bounds
    if region.orientation == "vertical":
        return max(1, x1 - x0)
    return max(1, y1 - y0)


def _should_merge(left: TextRegion, right: TextRegion) -> bool:
    if left.orientation != right.orientation:
        return False
    ax0, ay0, ax1, ay1 = left.bounds
    bx0, by0, bx1, by1 = right.bounds
    aw, ah = max(1, ax1 - ax0), max(1, ay1 - ay0)
    bw, bh = max(1, bx1 - bx0), max(1, by1 - by0)
    size_left = _char_size(left)
    size_right = _char_size(right)
    # 字号差异过大的区域（如标题与正文）不应合并。
    larger, smaller = max(size_left, size_right), max(1, min(size_left, size_right))
    if larger / smaller > 2.5:
        return False
    char_size = (size_left + size_right) / 2
    if left.orientation == "vertical":
        horizontal_gap = max(0, max(ax0, bx0) - min(ax1, bx1))
        vertical_overlap = _overlap(ay0, ay1, by0, by1)
        return horizontal_gap <= char_size * 1.8 and vertical_overlap >= min(ah, bh) * 0.2
    vertical_gap = max(0, max(ay0, by0) - min(ay1, by1))
    horizontal_overlap = _overlap(ax0, ax1, bx0, bx1)
    return vertical_gap <= char_size * 2.0 and horizontal_overlap >= min(aw, bw) * 0.15


def _groups_connected(group_a: list[TextRegion], group_b: list[TextRegion]) -> bool:
    return any(_should_merge(a, b) for a in group_a for b in group_b)


def merge_regions(regions: Sequence[TextRegion]) -> list[TextRegion]:
    groups: list[list[TextRegion]] = [[region] for region in regions]
    # 迭代合并直到稳定，确保间接相邻的区域也被归入同组。
    changed = True
    while changed:
        changed = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                if _groups_connected(groups[i], groups[j]):
                    groups[i].extend(groups[j])
                    del groups[j]
                    changed = True
                    break
            if changed:
                break

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


