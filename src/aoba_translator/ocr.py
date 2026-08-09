from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from .domain import TextRegion
from .messages import start_command_hint


class OcrError(RuntimeError):
    pass


class OcrEngine(Protocol):
    def recognize(self, image_path: Path) -> list[TextRegion]: ...


def _is_noise_text(text: str) -> bool:
    """纯 ASCII 单字符（如 "a"、"|"、"."）通常是误检；日文/中文字符保留。"""
    stripped = text.strip()
    if len(stripped) <= 1 and all(ord(char) < 128 for char in stripped):
        return True
    return False


def _resolve_gpu_and_threads(config: dict) -> bool:
    """解析 gpu 设置并在 CPU 模式下限制 torch 线程数（规避多线程卷积崩溃缺陷）。"""
    gpu_setting = config.get("gpu", "auto")
    gpu = gpu_setting
    if gpu_setting == "auto":
        try:
            import torch

            gpu = bool(torch.cuda.is_available())
        except ImportError:
            gpu = False
    if not gpu:
        # torch 2.13 系 CPU 后端在多线程卷积时存在随机性访问违例缺陷
        # （线程越多触发概率越高，会把整个服务进程杀死，且无任何 Python 报错）。
        # 实测本机 8/6 线程均会触发，2 线程稳定通过，故默认 2。
        # 可通过 ocr.cpu_threads 调整，但不建议超过 2；0 表示不限制（勿用）。
        try:
            import torch

            cpu_threads = int(config.get("cpu_threads", 2))
            if cpu_threads > 0:
                torch.set_num_threads(cpu_threads)
        except ImportError:
            pass
    return bool(gpu)


# ---------------------------------------------------------------------------
# 文字区域合并（供各引擎共用）
# ---------------------------------------------------------------------------


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
    # 合并后外接框面积不应远大于两者之和，避免把跨面板/跨气泡的区域框进同一块。
    union_area = (max(ax1, bx1) - min(ax0, bx0)) * (max(ay1, by1) - min(ay0, by0))
    if union_area > (aw * ah + bw * bh) * 1.7:
        return False
    char_size = (size_left + size_right) / 2
    if left.orientation == "vertical":
        horizontal_gap = max(0, max(ax0, bx0) - min(ax1, bx1))
        vertical_overlap = _overlap(ay0, ay1, by0, by1)
        return horizontal_gap <= char_size * 1.8 and vertical_overlap >= min(ah, bh) * 0.2
    vertical_gap = max(0, max(ay0, by0) - min(ay1, by1))
    horizontal_overlap = _overlap(ax0, ax1, bx0, bx1)
    return vertical_gap <= char_size * 2.0 and horizontal_overlap >= min(aw, bw) * 0.15


def _groups_connected(
    group_a: list[TextRegion], group_b: list[TextRegion], max_group_area: float
) -> bool:
    if max_group_area > 0:
        x0 = min(item.bounds[0] for item in (*group_a, *group_b))
        y0 = min(item.bounds[1] for item in (*group_a, *group_b))
        x1 = max(item.bounds[2] for item in (*group_a, *group_b))
        y1 = max(item.bounds[3] for item in (*group_a, *group_b))
        if (x1 - x0) * (y1 - y0) > max_group_area:
            return False
    return any(_should_merge(a, b) for a in group_a for b in group_b)


def _order_vertical(group: Sequence[TextRegion]) -> list[TextRegion]:
    """竖排多列文本排序：按从右到左的列阅读。

    直接按 (−x, y) 排序时，同一列内高低错开的框会被另一列的框插队，
    拼出的文本顺序错乱（翻译随之乱码）。先把水平重叠的框归入同一列
    （按重叠量取整），列按右→左排，列内按上→下排。
    """
    items = sorted(group, key=lambda item: -item.bounds[0])
    columns: list[list[TextRegion]] = []
    for item in items:
        _, y0, _, y1 = item.bounds
        best_column: list[TextRegion] | None = None
        best_overlap = 0
        for column in columns:
            col_y0 = min(member.bounds[1] for member in column)
            col_y1 = max(member.bounds[3] for member in column)
            overlap = _overlap(y0, y1, col_y0, col_y1)
            if overlap > best_overlap:
                best_overlap = overlap
                best_column = column
        if best_column is not None and best_overlap >= max(1, min(y1 - y0, 4)):
            best_column.append(item)
        else:
            columns.append([item])
    ordered: list[TextRegion] = []
    for column in sorted(
        columns, key=lambda column: -max(member.bounds[2] for member in column)
    ):
        ordered.extend(sorted(column, key=lambda item: item.bounds[1]))
    return ordered


def merge_regions(regions: Sequence[TextRegion], max_group_area: float = 0) -> list[TextRegion]:
    groups: list[list[TextRegion]] = [[region] for region in regions]
    # 迭代合并直到稳定，确保间接相邻的区域也被归入同组。
    changed = True
    while changed:
        changed = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                if _groups_connected(groups[i], groups[j], max_group_area):
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
            ordered = _order_vertical(group)
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


class EasyOcrEngine:
    name = "easyocr"

    def __init__(self, config: dict, model_dir: Path) -> None:
        try:
            import easyocr
        except ImportError as exc:
            raise OcrError(f"OCR 依赖未安装。{start_command_hint()}") from exc

        gpu = _resolve_gpu_and_threads(config)
        self._minimum_confidence = float(config.get("min_confidence", 0.25))
        self._min_region_size = int(config.get("min_region_size", 8))
        # 漫画场景检测参数：比 EasyOCR 默认值更敏感，减少漏检。
        self._text_threshold = float(config.get("text_threshold", 0.5))
        self._low_text = float(config.get("low_text", 0.3))
        self._link_threshold = float(config.get("link_threshold", 0.3))
        self._canvas_size = int(config.get("canvas_size", 2560))
        # 默认关闭量化：easyocr 的量化动态 LSTM 在较新 torch 的 CPU 后端上
        # 存在原生层崩溃缺陷（c10.dll 访问违例，直接杀死整个进程）。
        # 可通过 ocr.quantize=true 显式开启以换取少量速度提升。
        quantize = bool(config.get("quantize", False))
        self._reader = easyocr.Reader(
            list(config.get("languages", ["ja", "en"])),
            gpu=gpu,
            quantize=quantize,
            model_storage_directory=str(model_dir),
            download_enabled=False,
            verbose=False,
        )

    def recognize(self, image_path: Path) -> list[TextRegion]:
        try:
            results = self._reader.readtext(
                str(image_path),
                detail=1,
                paragraph=False,
                text_threshold=self._text_threshold,
                low_text=self._low_text,
                link_threshold=self._link_threshold,
                canvas_size=self._canvas_size,
            )
        except Exception as exc:
            raise OcrError(f"OCR 处理失败：{image_path.name}") from exc
        regions: list[TextRegion] = []
        for polygon, text, confidence in results:
            if float(confidence) < self._minimum_confidence or not str(text).strip():
                continue
            text = str(text).strip()
            if _is_noise_text(text):
                continue
            points = [(int(round(point[0])), int(round(point[1]))) for point in polygon]
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            width = max(xs) - min(xs)
            height = max(ys) - min(ys)
            # 噪音过滤：过小的区域和极细长的线条误检直接丢弃。
            if width < self._min_region_size or height < self._min_region_size:
                continue
            longest, shortest = max(width, height), max(1, min(width, height))
            if longest / shortest > 15:
                continue
            regions.append(
                TextRegion(
                    polygon=points,
                    text=text,
                    confidence=float(confidence),
                    orientation="vertical" if height > width * 1.2 else "horizontal",
                )
            )
        return regions


class HybridMangaOcrEngine:
    """用 EasyOCR CRAFT 检测文字区域，再用漫画专用模型 manga-ocr 逐区域识别。

    EasyOCR 的识别模型对漫画装饰字体置信度系统性偏低，而 manga-ocr
    （kha-white/manga-ocr-base）专为日文漫画训练，支持竖排与气泡字体。
    因此只借用 CRAFT 的检测能力，识别交给 manga-ocr。
    """

    name = "manga"

    def __init__(self, config: dict, model_dir: Path, manga_model_dir: Path | None = None) -> None:
        try:
            import easyocr
        except ImportError as exc:
            raise OcrError(f"OCR 依赖未安装。{start_command_hint()}") from exc

        gpu = _resolve_gpu_and_threads(config)
        self._min_region_size = int(config.get("min_region_size", 8))
        self._text_threshold = float(config.get("text_threshold", 0.5))
        self._low_text = float(config.get("low_text", 0.3))
        self._link_threshold = float(config.get("link_threshold", 0.3))
        self._canvas_size = int(config.get("canvas_size", 2560))
        self._crop_padding = float(config.get("crop_padding", 0.15))
        # 单个检测框面积超过整图的该比例时丢弃：这种框几乎必然横跨多个面板
        # 并覆盖画稿主体，识别拼接出的文本顺序错乱，擦除时也会涂抹画面。
        self._max_region_area_ratio = float(config.get("max_region_area_ratio", 0.12))
        quantize = bool(config.get("quantize", False))
        # recognizer=False：只加载 CRAFT 检测模型，不加载识别模型，节省内存。
        self._reader = easyocr.Reader(
            list(config.get("languages", ["ja", "en"])),
            gpu=gpu,
            quantize=quantize,
            recognizer=False,
            model_storage_directory=str(model_dir),
            download_enabled=False,
            verbose=False,
        )
        self._manga_model_dir = manga_model_dir
        self._mocr = None

    def _get_mocr(self):
        if self._mocr is None:
            try:
                from manga_ocr import MangaOcr
            except ImportError as exc:
                raise OcrError(f"manga-ocr 依赖未安装。{start_command_hint()}") from exc
            try:
                if self._manga_model_dir and (self._manga_model_dir / "config.json").exists():
                    self._mocr = MangaOcr(pretrained_model_name_or_path=str(self._manga_model_dir))
                else:
                    self._mocr = MangaOcr()
            except Exception as exc:
                raise OcrError(
                    f"manga-ocr 初始化失败：{exc}。若为网络问题，请在设置页重新执行初始化。"
                ) from exc
        return self._mocr

    def _detect_boxes(self, image_path: Path) -> list[list[tuple[int, int]]]:
        try:
            detections = self._reader.detect(
                str(image_path),
                text_threshold=self._text_threshold,
                low_text=self._low_text,
                link_threshold=self._link_threshold,
                canvas_size=self._canvas_size,
            )
        except Exception as exc:
            raise OcrError(f"OCR 检测失败：{image_path.name}") from exc
        # detect() 返回 (horizontal_list_agg, free_list_agg)，
        # 每个聚合对应一张输入图片，这里只处理单张。
        horizontal_agg, free_agg = detections
        horizontal_list = horizontal_agg[0] if horizontal_agg else []
        free_list = free_agg[0] if free_agg else []
        boxes: list[list[tuple[int, int]]] = []
        for box in horizontal_list or []:
            x0, x1, y0, y1 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
            boxes.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
        for quad in free_list or []:
            boxes.append([(int(round(p[0])), int(round(p[1]))) for p in quad])
        return boxes

    def recognize(self, image_path: Path) -> list[TextRegion]:
        boxes = self._detect_boxes(image_path)
        try:
            from PIL import Image
        except ImportError as exc:
            raise OcrError(f"图像处理依赖未安装。{start_command_hint()}") from exc
        image = Image.open(image_path).convert("RGB")
        image_area = max(1, image.width * image.height)
        max_area = image_area * self._max_region_area_ratio
        candidates: list[TextRegion] = []
        for points in boxes:
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            width = max(xs) - min(xs)
            height = max(ys) - min(ys)
            if width < self._min_region_size or height < self._min_region_size:
                continue
            if max(width, height) / max(1, min(width, height)) > 15:
                continue
            if width * height > max_area:
                continue
            candidates.append(
                TextRegion(
                    polygon=points,
                    text="",
                    confidence=1.0,
                    orientation="vertical" if height > width * 1.2 else "horizontal",
                )
            )
        merged = merge_regions(candidates, max_group_area=max_area)
        if not merged:
            return []

        mocr = self._get_mocr()
        regions: list[TextRegion] = []
        for region in merged:
            x0, y0, x1, y1 = region.bounds
            width, height = x1 - x0, y1 - y0
            pad = max(2, int(max(width, height) * self._crop_padding))
            crop = image.crop(
                (
                    max(0, x0 - pad),
                    max(0, y0 - pad),
                    min(image.width, x1 + pad),
                    min(image.height, y1 + pad),
                )
            )
            try:
                text = mocr(crop).strip()
            except Exception:
                text = ""
            if not text or _is_noise_text(text):
                continue
            region.text = text
            region.confidence = 1.0
            region.orientation = "vertical" if height > width * 1.2 else "horizontal"
            regions.append(region)
        return regions



