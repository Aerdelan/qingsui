from __future__ import annotations

import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import AppConfig
from .domain import JobRecord, JobStore
from .environment import inspect_environment
from .models import ModelManager
from .pipeline import TranslationPipeline


class JobManager:
    def __init__(
        self,
        config: AppConfig,
        model_manager: ModelManager,
        pipeline: TranslationPipeline,
        max_workers: int = 1,
    ) -> None:
        self.config = config
        self.models = model_manager
        self.pipeline = pipeline
        self.store = JobStore()
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="aoba-job")

    def submit_translation(self, input_path: Path, filename: str) -> JobRecord:
        record = JobRecord(
            id=uuid.uuid4().hex,
            kind="translation",
            filename=filename,
            input_path=input_path,
        )
        self.store.add(record)
        self.executor.submit(self._run_translation, record.id)
        return record

    def submit_setup(self) -> JobRecord:
        active_setup = next(
            (
                item
                for item in self.store.list()
                if item.kind == "setup" and item.status in {"queued", "running"}
            ),
            None,
        )
        if active_setup:
            return active_setup
        record = JobRecord(id=uuid.uuid4().hex, kind="setup", filename="本地模型初始化")
        self.store.add(record)
        self.executor.submit(self._run_setup, record.id)
        return record

    def _run_translation(self, job_id: str) -> None:
        record = self.store.get(job_id)
        if not record or not record.input_path:
            return
        self.store.update(job_id, status="running", progress=1, stage="启动翻译任务")
        try:
            output, detected, details = self.pipeline.run(
                record.input_path,
                job_id,
                lambda value, stage: self.store.update(
                    job_id, progress=value, stage=stage, message=""
                ),
                display_name=record.filename,
            )
            self.store.update(
                job_id,
                status="completed",
                progress=100,
                stage="处理完成",
                output_path=output,
                detected_type=detected,
                details=details,
            )
        except Exception as exc:
            self._write_failure(job_id, exc)

    def _run_setup(self, job_id: str) -> None:
        self.store.update(job_id, status="running", progress=1, stage="检查运行环境")
        try:
            report = inspect_environment()
            self.store.update(job_id, details={"environment": report.to_dict()})
            status = self.models.prepare(
                lambda value, stage: self.store.update(job_id, progress=value, stage=stage)
            )
            self.pipeline.reset_runtime()
            self.store.update(
                job_id,
                status="completed",
                progress=100,
                stage="初始化完成",
                details={"environment": report.to_dict(), "models": status},
            )
        except Exception as exc:
            self._write_failure(job_id, exc)

    def _write_failure(self, job_id: str, error: Exception) -> None:
        log_dir = self.config.local_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{job_id}.log"
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
        self.store.update(
            job_id,
            status="failed",
            stage="任务失败",
            message=str(error),
            details={"log": str(log_path)},
        )

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)

