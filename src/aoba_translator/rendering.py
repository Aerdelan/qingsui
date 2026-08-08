from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Sequence

from .domain import TextRegion
from .messages import start_command_hint


class RenderingError(RuntimeError):
    pass


def resolve_font(configured: str | None = None, weight: str = "normal") -> Path:
    if configured:
        path = Path(configured).expanduser()
        if path.exists() and path.is_file():
            return path
    windir = Path(os.environ.get("WINDIR", "C:/Windows"))
    regular = [
        windir / "Fonts" / "msyh.ttc",
        windir / "Fonts" / "msyhbd.ttc",
        windir / "Fonts" / "simhei.ttf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]
    bold = [
        windir / "Fonts" / "msyhbd.ttc",
        windir / "Fonts" / "msyh.ttc",
        windir / "Fonts" / "simhei.ttf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]
    candidates = bold if weight == "bold" else regular
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

    # 字重估算：粗体笔画更粗，字形填充率明显更高（在形态学膨胀前计算）。
    fill_ratio = int((glyph_mask > 0).sum()) / area
    weight = "bold" if fill_ratio > 0.45 else "normal"

    kernel = np.ones((3, 3), dtype=np.uint8)
    glyph_mask = cv2.morphologyEx(glyph_mask, cv2.MORPH_CLOSE, kernel)
    glyph_mask = cv2.dilate(glyph_mask, kernel, iterations=1)

    # 颜色采样：只取与背景色差最大的前 40% 像素，避免背景渗漏稀释文字色。
    selected = crop[glyph_mask > 0]
    if selected.size:
        diffs = np.linalg.norm(selected.astype(np.float32) - background, axis=1)
        keep = max(1, int(diffs.size * 0.4))
        indices = np.argpartition(diffs, -keep)[-keep:]
        color = tuple(int(value) for value in np.median(selected[indices], axis=0))
    else:
        background_luma = float(background.mean())
        color = (25, 25, 25) if background_luma > 128 else (245, 245, 245)
    # 对比度保护：文字色与背景色过近时强制使用深/浅色。
    if math.dist(color, tuple(int(value) for value in background)) < 30:
        color = (25, 25, 25) if float(background.mean()) > 128 else (245, 245, 245)
    return (x0, y0, x1, y1), glyph_mask, color, tuple(int(value) for value in background), weight


def erase_original_text(image, regions: Sequence[TextRegion], padding: int, radius: int):
    import cv2
    import numpy as np

    full_mask = np.zeros(image.shape[:2], dtype=np.uint8)
    background_colors: list[tuple[int, int, int]] = []
    for region in regions:
        bounds, local_mask, color, background, weight = _text_mask_and_color(image, region, padding)
        x0, y0, x1, y1 = bounds
        full_mask[y0 : y1 + 1, x0 : x1 + 1] = np.maximum(
            full_mask[y0 : y1 + 1, x0 : x1 + 1], local_mask
        )
        region.text_color = color
        region.weight = weight
        region.background_color = background
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
    text = text.replace("\r", "").replace("\n", "")
    # 短文本（1-3 字）保持单行，不强制换行。
    if len(text) <= 3:
        return [text] if text else [""]
    lines: list[str] = []
    current = ""
    for character in text:
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
    line_height = max(int(font.size * 1.25), _measure(draw, "国Ag", font)[1] + 2)
    total_height = line_height * len(lines)
    widest = max((_measure(draw, line, font)[0] for line in lines), default=0)
    return lines, widest, total_height, line_height


def _vertical_layout(text: str, font_size: int, width: int, height: int):
    characters = [character for character in text.replace("\r", "").replace("\n", "") if character]
    cell = max(1, int(font_size * 1.08))
    rows = max(1, height // cell)
    columns = [characters[index : index + rows] for index in range(0, len(characters), rows)]
    return columns, len(columns) * cell, min(rows, len(characters)) * cell, cell


def _estimate_original_size(region: TextRegion, bounds: tuple[int, int, int, int]) -> int:
    """按区域面积与字数估算原文字号（CJK 字符近似方形）。"""
    x0, y0, x1, y1 = bounds
    width = max(4, x1 - x0)
    height = max(4, y1 - y0)
    text_len = max(1, len(region.text))
    square = math.sqrt(width * height / text_len)
    if region.orientation == "vertical":
        return int(min(width, max(6.0, square)))
    return int(min(height, max(6.0, square)))


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


def render_translations(image, regions: Sequence[TextRegion], configured_font: str | None, config: dict):
    from PIL import Image, ImageDraw

    canvas = Image.fromarray(image)
    draw = ImageDraw.Draw(canvas)
    minimum = int(config.get("font_min_size", 12))
    maximum = int(config.get("font_max_size", 72))

    for region in regions:
        text = region.translated_text.strip()
        if not text:
            continue
        font_path = (
            Path(configured_font).expanduser()
            if configured_font
            else resolve_font(None, region.weight)
        )
        x0, y0, x1, y1 = region.bounds
        inset = max(1, int(min(x1 - x0, y1 - y0) * 0.03))
        bounds = (x0 + inset, y0 + inset, x1 - inset, y1 - inset)
        width = max(4, bounds[2] - bounds[0])
        height = max(4, bounds[3] - bounds[1])

        # 字号策略：以原文估算字号为目标，上限为其 1.2 倍；
        # 放不下时二分缩小，最小字号仍放不下则允许边界外扩 10%。
        estimate = _estimate_original_size(region, bounds)
        cap = min(maximum, max(minimum, int(estimate * 1.2)))
        font = _fit_font(draw, text, font_path, bounds, region.orientation, minimum, cap)
        if region.orientation == "vertical":
            _, used_width, used_height, _ = _vertical_layout(text, font.size, width, height)
        else:
            _, used_width, used_height, _ = _horizontal_layout(draw, text, font, width, height)
        if used_width > width or used_height > height:
            grow_x = max(1, int(width * 0.05))
            grow_y = max(1, int(height * 0.05))
            bounds = (
                max(0, bounds[0] - grow_x),
                max(0, bounds[1] - grow_y),
                bounds[2] + grow_x,
                bounds[3] + grow_y,
            )
            width = max(4, bounds[2] - bounds[0])
            height = max(4, bounds[3] - bounds[1])

        fill = tuple(region.text_color)
        # 文字色与背景色差过小时添加描边，提高复杂背景上的可读性。
        contrast = math.dist(fill, tuple(region.background_color))
        stroke_width = max(1, font.size // 16) if contrast < 60 else 0
        stroke_fill = tuple(region.background_color) if stroke_width else None

        # 以原文多边形重心为排版锚点，避免强制居中导致位置偏移。
        points = region.polygon or [(bounds[0], bounds[1]), (bounds[2], bounds[3])]
        cx = sum(point[0] for point in points) / len(points)
        cy = sum(point[1] for point in points) / len(points)
        anchor_x = min(1.0, max(0.0, (cx - bounds[0]) / max(1, width)))
        anchor_y = min(1.0, max(0.0, (cy - bounds[1]) / max(1, height)))

        if region.orientation == "vertical":
            columns, used_width, used_height, cell = _vertical_layout(
                text, font.size, width, height
            )
            start_x = bounds[0] + max(0, int((width - used_width) * anchor_x)) + used_width - cell
            for column_index, column in enumerate(columns):
                x = start_x - column_index * cell
                column_height = len(column) * cell
                y = bounds[1] + max(0, int((height - column_height) * anchor_y))
                for row_index, character in enumerate(column):
                    character_width, character_height = _measure(draw, character, font)
                    draw.text(
                        (x + (cell - character_width) / 2, y + row_index * cell + (cell - character_height) / 2),
                        character,
                        font=font,
                        fill=fill,
                        stroke_width=stroke_width,
                        stroke_fill=stroke_fill,
                    )
        else:
            lines, _, total_height, line_height = _horizontal_layout(draw, text, font, width, height)
            y = bounds[1] + max(0, int((height - total_height) * anchor_y))
            for line in lines:
                line_width, _ = _measure(draw, line, font)
                draw.text(
                    (bounds[0] + max(0, int((width - line_width) * anchor_x)), y),
                    line,
                    font=font,
                    fill=fill,
                    stroke_width=stroke_width,
                    stroke_fill=stroke_fill,
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
    rendered = render_translations(restored, regions, config.get("font_path"), config)
    destination.parent.mkdir(parents=True, exist_ok=True)
    extension = destination.suffix.lower()
    if extension in {".jpg", ".jpeg"}:
        rendered.save(destination, quality=95, subsampling=0)
    else:
        rendered.save(destination)
