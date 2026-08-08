from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Sequence

from .domain import TextRegion
from .messages import start_command_hint


class RenderingError(RuntimeError):
    pass


def resolve_font(configured: str | None = None) -> Path:
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    windir = Path(os.environ.get("WINDIR", "C:/Windows"))
    candidates.extend(
        [
            windir / "Fonts" / "msyh.ttc",
            windir / "Fonts" / "msyhbd.ttc",
            windir / "Fonts" / "simhei.ttf",
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
            Path("/System/Library/Fonts/PingFang.ttc"),
        ]
    )
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    raise RenderingError("未找到中文字体，请在配置 rendering.font_path 中指定字体文件。")


def _polygon_mask(width: int, height: int, polygon: Sequence[tuple[int, int]]):
    import cv2
    import numpy as np

    mask = np.zeros((height, width), dtype=np.uint8)
    points = np.array(polygon, dtype=np.int32)
    cv2.fillPoly(mask, [points], 255)
    return mask


def _estimate_background(crop):
    import numpy as np

    if crop.size == 0:
        return np.array([255, 255, 255], dtype=np.float32)
    top = crop[0, :, :]
    bottom = crop[-1, :, :]
    left = crop[:, 0, :]
    right = crop[:, -1, :]
    border = np.concatenate((top, bottom, left, right), axis=0)
    return np.median(border.astype(np.float32), axis=0)


def _text_mask_and_color(image, region: TextRegion, padding: int):
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    x0, y0, x1, y1 = region.bounds
    x0 = max(0, x0 - padding)
    y0 = max(0, y0 - padding)
    x1 = min(width - 1, x1 + padding)
    y1 = min(height - 1, y1 + padding)
    crop = image[y0 : y1 + 1, x0 : x1 + 1]
    local_polygon = [(x - x0, y - y0) for x, y in region.polygon]
    polygon_mask = _polygon_mask(crop.shape[1], crop.shape[0], local_polygon)
    background = _estimate_background(crop)
    difference = np.linalg.norm(crop.astype(np.float32) - background, axis=2)
    candidates = difference[polygon_mask > 0]
    if candidates.size:
        threshold = max(24.0, float(np.percentile(candidates, 58)))
    else:
        threshold = 24.0
    glyph_mask = ((difference >= threshold) & (polygon_mask > 0)).astype(np.uint8) * 255

    area = max(1, int((polygon_mask > 0).sum()))
    if int((glyph_mask > 0).sum()) < area * 0.015:
        gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
        background_luma = float(background.mean())
        if background_luma > 128:
            fallback = gray < max(0, int(background_luma - 28))
        else:
            fallback = gray > min(255, int(background_luma + 28))
        glyph_mask = (fallback & (polygon_mask > 0)).astype(np.uint8) * 255

    kernel = np.ones((3, 3), dtype=np.uint8)
    glyph_mask = cv2.morphologyEx(glyph_mask, cv2.MORPH_CLOSE, kernel)
    glyph_mask = cv2.dilate(glyph_mask, kernel, iterations=1)
    selected = crop[glyph_mask > 0]
    if selected.size:
        color = tuple(int(value) for value in np.median(selected, axis=0))
    else:
        background_luma = float(background.mean())
        color = (25, 25, 25) if background_luma > 128 else (245, 245, 245)
    return (x0, y0, x1, y1), glyph_mask, color, tuple(int(value) for value in background)


def erase_original_text(image, regions: Sequence[TextRegion], padding: int, radius: int):
    import cv2
    import numpy as np

    full_mask = np.zeros(image.shape[:2], dtype=np.uint8)
    background_colors: list[tuple[int, int, int]] = []
    for region in regions:
        bounds, local_mask, color, background = _text_mask_and_color(image, region, padding)
        x0, y0, x1, y1 = bounds
        full_mask[y0 : y1 + 1, x0 : x1 + 1] = np.maximum(
            full_mask[y0 : y1 + 1, x0 : x1 + 1], local_mask
        )
        region.text_color = color
        background_colors.append(background)
    if not full_mask.any():
        return image.copy(), background_colors
    restored = cv2.inpaint(image, full_mask, float(radius), cv2.INPAINT_TELEA)
    return restored, background_colors


def _measure(draw, text: str, font) -> tuple[int, int]:
    if not text:
        return 0, 0
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _wrap_horizontal(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in text.replace("\r", "").replace("\n", ""):
        candidate = current + character
        if current and _measure(draw, candidate, font)[0] > max_width:
            lines.append(current)
            current = character
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _horizontal_layout(draw, text: str, font, width: int, height: int):
    lines = _wrap_horizontal(draw, text, font, width)
    line_height = max(_measure(draw, "国Ag", font)[1], font.size) + max(1, font.size // 8)
    total_height = line_height * len(lines)
    widest = max((_measure(draw, line, font)[0] for line in lines), default=0)
    return lines, widest, total_height, line_height


def _vertical_layout(text: str, font_size: int, width: int, height: int):
    characters = [character for character in text.replace("\r", "").replace("\n", "") if character]
    cell = max(1, int(font_size * 1.08))
    rows = max(1, height // cell)
    columns = [characters[index : index + rows] for index in range(0, len(characters), rows)]
    return columns, len(columns) * cell, min(rows, len(characters)) * cell, cell


def _fit_font(draw, text: str, font_path: Path, bounds, orientation: str, minimum: int, maximum: int):
    from PIL import ImageFont

    x0, y0, x1, y1 = bounds
    width = max(4, x1 - x0)
    height = max(4, y1 - y0)
    low = minimum
    high = max(minimum, maximum)
    best = ImageFont.truetype(str(font_path), minimum)
    while low <= high:
        size = (low + high) // 2
        font = ImageFont.truetype(str(font_path), size)
        if orientation == "vertical":
            _, used_width, used_height, _ = _vertical_layout(text, size, width, height)
        else:
            _, used_width, used_height, _ = _horizontal_layout(draw, text, font, width, height)
        if used_width <= width and used_height <= height:
            best = font
            low = size + 1
        else:
            high = size - 1
    return best


def render_translations(image, regions: Sequence[TextRegion], font_path: Path, config: dict):
    from PIL import Image, ImageDraw

    canvas = Image.fromarray(image)
    draw = ImageDraw.Draw(canvas)
    minimum = int(config.get("font_min_size", 12))
    maximum = int(config.get("font_max_size", 72))

    for region in regions:
        text = region.translated_text.strip()
        if not text:
            continue
        x0, y0, x1, y1 = region.bounds
        inset = max(1, int(min(x1 - x0, y1 - y0) * 0.03))
        bounds = (x0 + inset, y0 + inset, x1 - inset, y1 - inset)
        font = _fit_font(draw, text, font_path, bounds, region.orientation, minimum, maximum)
        width = max(4, bounds[2] - bounds[0])
        height = max(4, bounds[3] - bounds[1])
        fill = tuple(region.text_color)

        if region.orientation == "vertical":
            columns, used_width, used_height, cell = _vertical_layout(
                text, font.size, width, height
            )
            start_x = bounds[0] + max(0, (width - used_width) // 2) + used_width - cell
            for column_index, column in enumerate(columns):
                x = start_x - column_index * cell
                y = bounds[1] + max(0, (height - len(column) * cell) // 2)
                for row_index, character in enumerate(column):
                    character_width, character_height = _measure(draw, character, font)
                    draw.text(
                        (x + (cell - character_width) / 2, y + row_index * cell + (cell - character_height) / 2),
                        character,
                        font=font,
                        fill=fill,
                    )
        else:
            lines, _, total_height, line_height = _horizontal_layout(draw, text, font, width, height)
            y = bounds[1] + max(0, (height - total_height) // 2)
            for line in lines:
                line_width, _ = _measure(draw, line, font)
                draw.text(
                    (bounds[0] + max(0, (width - line_width) // 2), y),
                    line,
                    font=font,
                    fill=fill,
                )
                y += line_height
    return canvas


def process_image_render(
    source: Path,
    destination: Path,
    regions: Sequence[TextRegion],
    config: dict,
) -> None:
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise RenderingError(f"图像处理依赖未安装。{start_command_hint()}") from exc

    with Image.open(source) as opened:
        rgb = opened.convert("RGB")
        image = np.array(rgb)
    restored, _ = erase_original_text(
        image,
        regions,
        padding=int(config.get("mask_padding", 3)),
        radius=int(config.get("inpaint_radius", 3)),
    )
    font_path = resolve_font(config.get("font_path"))
    rendered = render_translations(restored, regions, font_path, config)
    destination.parent.mkdir(parents=True, exist_ok=True)
    extension = destination.suffix.lower()
    if extension in {".jpg", ".jpeg"}:
        rendered.save(destination, quality=95, subsampling=0)
    else:
        rendered.save(destination)


