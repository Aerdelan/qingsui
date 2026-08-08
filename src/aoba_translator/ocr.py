from __future__ import annotations

from pathlib import Path

from .domain import TextRegion
from .messages import start_command_hint


class OcrError(RuntimeError):
    pass


def _is_noise_text(text: str) -> bool:
    """纯 ASCII 单字符（如 "a"、"|"、"."）通常是误检；日文/中文字符保留。"""
    stripped = text.strip()
    if len(stripped) <= 1 and all(ord(char) < 128 for char in stripped):
        return True
    return False


class EasyOcrEngine:
    def __init__(self, config: dict, model_dir: Path) -> None:
        try:
            import easyocr
        except ImportError as exc:
            raise OcrError(f"OCR 依赖未安装。{start_command_hint()}") from exc

        gpu_setting = config.get("gpu", "auto")
        gpu = gpu_setting
        if gpu_setting == "auto":
            try:
                import torch

                gpu = bool(torch.cuda.is_available())
            except ImportError:
                gpu = False
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



