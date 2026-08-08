from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Callable

from .archive import (
    ARCHIVE_EXTENSIONS,
    IMAGE_EXTENSIONS,
    TEXT_EXTENSIONS,
    classify_files,
    classify_input,
    collect_content_files,
    create_output_archive,
    extract_archive,
)
from .config import AppConfig
from .manga import translate_manga_images
from .models import ModelManager
from .novel import translate_novel
from .ocr import EasyOcrEngine
from .translation import Translator, build_translator


ProgressCallback = Callable[[int, str], None]


class PipelineError(RuntimeError):
    pass


class TranslationPipeline:
    def __init__(self, config: AppConfig, model_manager: ModelManager) -> None:
        self.config = config
        self.model_manager = model_manager
        self._translator: Translator | None = None
        self._ocr: EasyOcrEngine | None = None
        self._runtime_lock = Lock()

    def reset_runtime(self) -> None:
        with self._runtime_lock:
            self._translator = None
            self._ocr = None

    def _get_translator(self) -> Translator:
        with self._runtime_lock:
            if self._translator is None:
                self._translator = build_translator(
                    self.config.section("translation"), self.config.models_dir
                )
            return self._translator

    def _get_ocr(self) -> EasyOcrEngine:
        with self._runtime_lock:
            if self._ocr is None:
                self._ocr = EasyOcrEngine(
                    self.config.section("ocr"), self.model_manager.ocr_dir
                )
            return self._ocr

    def run(
        self,
        input_path: Path,
        job_id: str,
        progress: ProgressCallback | None = None,
        *,
        display_name: str | None = None,
    ) -> tuple[Path, str, dict]:
        def report(value: int, stage: str) -> None:
            if progress:
                progress(max(0, min(100, value)), stage)

        report(2, "检查输入文件")
        logical_name = display_name or input_path.name
        input_kind = classify_input(input_path)
        job_root = self.config.work_dir / job_id
        extracted_dir = job_root / "extracted"
        product_dir = job_root / "product"
        if job_root.exists():
            shutil.rmtree(job_root)
        extracted_dir.mkdir(parents=True, exist_ok=True)
        product_dir.mkdir(parents=True, exist_ok=True)

        limits = self.config.section("limits")
        if input_kind == "archive":
            report(5, "安全解压压缩包")
            files = extract_archive(
                input_path,
                extracted_dir,
                max_files=int(limits.get("max_archive_files", 5000)),
                max_total_bytes=int(limits.get("max_extracted_mb", 4096)) * 1024**2,
            )
            detected = classify_files(files)
            content_root = extracted_dir
        else:
            detected = input_kind
            copied = extracted_dir / logical_name
            shutil.copy2(input_path, copied)
            files = [copied]
            content_root = extracted_dir

        report(12, "加载本地翻译模型")
        translator = self._get_translator()
        if detected == "novel":
            details = self._process_novels(
                files, content_root, product_dir, translator, report
            )
        elif detected == "manga":
            details = self._process_manga(
                files, content_root, product_dir, translator, report
            )
        else:
            raise PipelineError(f"无法处理输入类型：{detected}")

        report(94, "生成处理报告")
        report_path = product_dir / "translation-report.json"
        report_path.write_text(
            json.dumps(
                {
                    "product": "Aoba Translator",
                    "source": logical_name,
                    "detected_type": detected,
                    "translation_provider": translator.name,
                    "created_at": datetime.now().astimezone().isoformat(),
                    "details": details,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        report(97, "打包最终产物")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_stem = "".join(
            character if character.isalnum() or character in "-_." else "_"
            for character in Path(logical_name).stem
        ).strip("._") or "translation"
        archive_path = self.config.output_dir / f"{timestamp}_{safe_stem}_{job_id[:8]}_zh.zip"
        create_output_archive(product_dir, archive_path)
        report(100, "翻译产物已生成")
        return archive_path, detected, details

    def _process_novels(
        self,
        files: list[Path],
        content_root: Path,
        product_dir: Path,
        translator: Translator,
        report: ProgressCallback,
    ) -> dict:
        text_files = [path for path in files if path.suffix.lower() in TEXT_EXTENSIONS]
        if not text_files:
            raise PipelineError("未找到小说文本文件。")
        translation = self.config.section("translation")
        entries: list[dict] = []
        for index, source in enumerate(text_files, start=1):
            relative = source.relative_to(content_root)
            destination = product_dir / relative.parent / f"{source.stem}_zh{source.suffix}"

            def item_progress(value: int, message: str) -> None:
                overall = 18 + int(((index - 1) + value / 100) / len(text_files) * 72)
                report(overall, message)

            metadata = translate_novel(
                source,
                destination,
                translator,
                max_chars=int(translation.get("max_input_chars", 420)),
                batch_size=int(translation.get("batch_size", 8)),
                context_segments=int(translation.get("context_segments", 4)),
                context_chars=int(translation.get("context_chars", 1800)),
                progress=item_progress,
            )
            entries.append({"file": relative.as_posix(), **metadata})
        return {"documents": entries, "count": len(entries)}

    def _process_manga(
        self,
        files: list[Path],
        content_root: Path,
        product_dir: Path,
        translator: Translator,
        report: ProgressCallback,
    ) -> dict:
        image_files = [path for path in files if path.suffix.lower() in IMAGE_EXTENSIONS]
        if not image_files:
            raise PipelineError("未找到漫画图片。")
        pairs = [(source, product_dir / source.relative_to(content_root)) for source in image_files]
        ocr = self._get_ocr()

        def manga_progress(value: int, message: str) -> None:
            report(18 + int(value * 0.72), message)

        pages = translate_manga_images(
            pairs,
            ocr,
            translator,
            self.config.section("rendering"),
            batch_size=int(self.config.section("translation").get("batch_size", 8)),
            context_chars=int(self.config.section("translation").get("context_chars", 1800)),
            progress=manga_progress,
        )
        return {
            "pages": pages,
            "count": len(pages),
            "recognized_regions": sum(item["regions"] for item in pages),
        }



