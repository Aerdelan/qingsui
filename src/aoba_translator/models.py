from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .messages import missing_dependencies_message

try:
    from huggingface_hub.utils import tqdm as _HfTqdm
except ImportError:  # huggingface-hub 未安装时不影响模块导入
    _HfTqdm = None

# 国内访问 GitHub 不稳定，可为 EasyOCR 模型下载指定镜像前缀。
# 支持 config.json 中 ocr.model_mirror 或环境变量 AOBA_EASYOCR_MIRROR，
# 例如 "https://ghfast.top/"，会拼接在原始 GitHub 地址前面。
_EASYOCR_MIRROR_KEY = "model_mirror"


class ModelSetupError(RuntimeError):
    pass


ProgressCallback = Callable[[int, str], None]


class ModelManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.translation_dir = config.models_dir / "translation"
        self.ocr_dir = config.models_dir / "easyocr"
        self.manga_ocr_dir = config.models_dir / "manga_ocr"

    def _translation_ready(self) -> bool:
        if not (self.translation_dir / "config.json").exists():
            return False
        return any(self.translation_dir.glob("pytorch_model*.bin")) or any(
            self.translation_dir.glob("model*.safetensors")
        )

    def _ocr_ready(self) -> bool:
        model_files = list(self.ocr_dir.glob("*.pth")) + list(self.ocr_dir.glob("*.pt"))
        return len(model_files) >= 2

    def _manga_ocr_ready(self) -> bool:
        if not (self.manga_ocr_dir / "config.json").exists():
            return False
        return any(self.manga_ocr_dir.glob("pytorch_model*.bin")) or any(
            self.manga_ocr_dir.glob("model*.safetensors")
        )

    def _ocr_provider(self) -> str:
        return str(self.config.section("ocr").get("provider", "manga")).lower()

    def _ollama_model_ready(self, model: str) -> bool:
        executable = shutil.which("ollama")
        if not executable:
            return False
        try:
            completed = subprocess.run(
                [executable, "list"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if completed.returncode != 0:
            return False
        installed = {
            line.split()[0]
            for line in completed.stdout.splitlines()[1:]
            if line.split()
        }
        return model in installed or (
            ":" not in model and any(name.startswith(model + ":") for name in installed)
        )

    def status(self) -> dict[str, Any]:
        translation = self.config.section("translation")
        provider = str(translation.get("provider", "ollama")).lower()
        if provider == "transformers":
            translation_ready = self._translation_ready()
        elif provider == "ollama":
            translation_ready = self._ollama_model_ready(
                str(translation.get("ollama_model", "qwen3.5:2b"))
            )
        else:
            translation_ready = provider == "echo"

        ocr_ready = self._ocr_ready()
        ocr_provider = self._ocr_provider()
        if ocr_provider == "manga":
            ocr_ready = ocr_ready and self._manga_ocr_ready()
        dependency_names = ["easyocr", "cv2", "numpy", "PIL", "torch"]
        if ocr_provider == "manga":
            dependency_names.append("manga_ocr")
        if provider == "transformers":
            dependency_names.append("transformers")
        dependencies = {
            name: importlib.util.find_spec(name) is not None for name in dependency_names
        }
        runtime = self.config.load_runtime()
        return {
            "ready": translation_ready and ocr_ready and all(dependencies.values()),
            "translation_ready": translation_ready,
            "ocr_ready": ocr_ready,
            "ocr_provider": ocr_provider,
            "dependencies": dependencies,
            "translation_provider": provider,
            "translation_model": translation.get("model_id")
            if provider == "transformers"
            else translation.get("ollama_model"),
            "last_setup_at": runtime.get("last_setup_at"),
            "setup_error": runtime.get("setup_error"),
        }

    def prepare(self, progress: ProgressCallback | None = None) -> dict[str, Any]:
        def report(value: int, message: str) -> None:
            if progress:
                progress(value, message)

        required = ("easyocr", "cv2", "numpy", "PIL", "torch")
        missing = [name for name in required if importlib.util.find_spec(name) is None]
        provider = str(self.config.section("translation").get("provider", "ollama")).lower()
        if provider == "transformers" and importlib.util.find_spec("transformers") is None:
            missing.append("transformers")
        if self._ocr_provider() == "manga" and importlib.util.find_spec("manga_ocr") is None:
            missing.append("manga_ocr")
        if missing:
            message = missing_dependencies_message(missing)
            self._save_error(message)
            raise ModelSetupError(message)

        try:
            report(5, "检查本地模型目录")
            self.translation_dir.mkdir(parents=True, exist_ok=True)
            self.ocr_dir.mkdir(parents=True, exist_ok=True)
            if provider == "transformers":
                self._prepare_transformers(report)
            elif provider == "ollama":
                self._prepare_ollama(report)
            elif provider != "echo":
                raise ModelSetupError(f"未知翻译提供器：{provider}")

            self._prepare_easyocr(report)
            if self._ocr_provider() == "manga":
                self._prepare_manga_ocr(report)
            runtime = self.config.load_runtime()
            runtime.update(
                {
                    "last_setup_at": datetime.now(timezone.utc).isoformat(),
                    "setup_error": None,
                    "translation_provider": provider,
                    "translation_model": self.config.section("translation").get("model_id"),
                }
            )
            self.config.save_runtime(runtime)
            report(100, "模型初始化完成")
            return self.status()
        except Exception as exc:
            self._save_error(str(exc))
            if isinstance(exc, ModelSetupError):
                raise
            raise ModelSetupError(f"模型初始化失败：{exc}") from exc

    def _prepare_transformers(self, report: ProgressCallback) -> None:
        if self._translation_ready():
            report(45, "翻译模型已存在")
            return
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise ModelSetupError("缺少 huggingface-hub，无法下载翻译模型。") from exc

        model_id = str(
            self.config.section("translation").get(
                "model_id", "shun89/opus-mt-ja-zh"
            )
        )
        report(15, f"准备下载翻译模型 {model_id}")
        os.environ.setdefault("HF_HOME", str(self.config.models_dir / "huggingface"))
        tracker = _DownloadTracker(model_id, report)
        download_kwargs: dict[str, Any] = {}
        if _HfTqdm is not None:
            download_kwargs["tqdm_class"] = tracker.make_bar_class()
        snapshot_download(
            repo_id=model_id,
            local_dir=str(self.translation_dir),
            ignore_patterns=("*.h5", "*.msgpack", "*.ot"),
            **download_kwargs,
        )
        report(50, "翻译模型下载完成")

    def _prepare_ollama(self, report: ProgressCallback) -> None:
        executable = shutil.which("ollama")
        if not executable:
            raise ModelSetupError(
                "未找到 Ollama。请先执行：winget install --id Ollama.Ollama --exact，"
                "安装完成后重新运行 start.ps1。"
            )
        model = str(self.config.section("translation").get("ollama_model", "qwen3.5:2b"))
        if self._ollama_model_ready(model):
            report(50, f"Ollama 模型 {model} 已存在")
            return
        report(15, f"拉取 Ollama 模型 {model}")
        completed = subprocess.run(
            [executable, "pull", model],
            capture_output=True,
            text=True,
            timeout=7200,
            check=False,
        )
        if completed.returncode != 0:
            raise ModelSetupError(completed.stderr.strip() or "Ollama 模型拉取失败。")
        report(50, "Ollama 模型准备完成")

    def _prepare_easyocr(self, report: ProgressCallback) -> None:
        if self._ocr_ready():
            report(90, "OCR 模型已存在")
            return
        import easyocr

        ocr = self.config.section("ocr")
        gpu_setting = ocr.get("gpu", "auto")
        if gpu_setting == "auto":
            import torch

            gpu_setting = bool(torch.cuda.is_available())
        mirror = str(
            ocr.get(_EASYOCR_MIRROR_KEY)
            or os.environ.get("AOBA_EASYOCR_MIRROR", "")
            or ""
        ).strip()
        if mirror:
            if not mirror.endswith("/"):
                mirror += "/"
            self._apply_easyocr_mirror(easyocr, mirror)
            report(60, f"下载日文 OCR 模型（镜像：{mirror.rstrip('/')}）")
        else:
            report(
                60,
                "下载日文 OCR 模型（源为 GitHub，如卡住可在配置 ocr.model_mirror 中设置镜像）",
            )
        easyocr.Reader(
            list(ocr.get("languages", ["ja", "en"])),
            gpu=gpu_setting,
            model_storage_directory=str(self.ocr_dir),
            download_enabled=True,
            verbose=False,
        )
        report(95, "OCR 模型下载完成")

    def _prepare_manga_ocr(self, report: ProgressCallback) -> None:
        """下载漫画专用识别模型 kha-white/manga-ocr-base（约 430MB）。"""
        if self._manga_ocr_ready():
            report(96, "漫画 OCR 模型已存在")
            return
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise ModelSetupError("缺少 huggingface-hub，无法下载漫画 OCR 模型。") from exc

        model_id = str(self.config.section("ocr").get("manga_model_id", "kha-white/manga-ocr-base"))
        report(95, f"准备下载漫画 OCR 模型 {model_id}")
        os.environ.setdefault("HF_HOME", str(self.config.models_dir / "huggingface"))
        self.manga_ocr_dir.mkdir(parents=True, exist_ok=True)
        tracker = _DownloadTracker(model_id, report, start=95, end=99)
        download_kwargs: dict[str, Any] = {}
        if _HfTqdm is not None:
            download_kwargs["tqdm_class"] = tracker.make_bar_class()
        try:
            snapshot_download(
                repo_id=model_id,
                local_dir=str(self.manga_ocr_dir),
                ignore_patterns=("*.h5", "*.msgpack", "*.ot"),
                **download_kwargs,
            )
        except Exception as exc:
            raise ModelSetupError(
                f"漫画 OCR 模型下载失败：{exc}。若为网络问题，可设置环境变量 HF_ENDPOINT "
                "（如 https://hf-mirror.com）后在设置页重新执行初始化。"
            ) from exc
        report(99, "漫画 OCR 模型下载完成")

    @staticmethod
    def _apply_easyocr_mirror(easyocr_module: Any, mirror: str) -> None:
        """把 EasyOCR 内置模型下载地址改写为镜像地址，仅修改进程内存中的配置。"""
        from easyocr import config as easyocr_config

        def _rewrite(node: Any) -> None:
            if isinstance(node, dict):
                url = node.get("url")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    node["url"] = mirror + url
                for value in node.values():
                    _rewrite(value)

        for collection in (
            getattr(easyocr_config, "detection_models", None),
            getattr(easyocr_config, "recognition_models", None),
        ):
            _rewrite(collection)

    def _save_error(self, message: str) -> None:
        runtime = self.config.load_runtime()
        runtime["setup_error"] = message
        self.config.save_runtime(runtime)


class _DownloadTracker:
    """聚合多个并发下载文件的进度，向任务状态回报实时百分比与速度。"""

    def __init__(
        self,
        model_id: str,
        report: ProgressCallback,
        start: int = 15,
        end: int = 50,
    ) -> None:
        self.model_id = model_id
        self.report = report
        self.start = start
        self.end = end
        self._lock = threading.Lock()
        self._bars: list[Any] = []
        self._last_emit_at = 0.0
        self._samples: list[tuple[float, int]] = []

    def _register(self, bar: Any) -> None:
        with self._lock:
            self._bars.append(bar)

    def _on_change(self, force: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_emit_at < 0.8:
                return
            downloaded = sum(getattr(bar, "_tracked_bytes", 0) for bar in self._bars)
            total = sum(
                int(bar.total) for bar in self._bars if getattr(bar, "total", None)
            )
            self._samples = [
                (t, b) for t, b in self._samples if now - t <= 4.0
            ]
            self._samples.append((now, downloaded))
            speed = 0.0
            if len(self._samples) >= 2:
                first_time, first_bytes = self._samples[0]
                last_time, last_bytes = self._samples[-1]
                window = last_time - first_time
                if window > 0:
                    speed = max(0.0, (last_bytes - first_bytes) / window)
            self._last_emit_at = now
        if total > 0:
            percent = min(100, downloaded * 100 // total)
            value = min(self.end, self.start + (self.end - self.start) * downloaded // total)
            size_text = f"{self._format_size(downloaded)}/{self._format_size(total)}"
            message = f"正在下载翻译模型 {self.model_id}（{percent}% · {size_text}）"
        else:
            value = self.start
            message = f"正在下载翻译模型 {self.model_id}（已下载 {self._format_size(downloaded)}）"
        if speed > 0:
            message += f" · {self._format_speed(speed)}"
        self.report(value, message)

    def make_bar_class(self) -> type:
        tracker = self

        class _TrackedBar(_HfTqdm):  # type: ignore[misc, valid-type]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs.setdefault("mininterval", 1.0)
                super().__init__(*args, **kwargs)
                self._tracked_bytes = 0
                # 多文件下载时 snapshot_download 会创建多个进度条：
                # 只跟踪字节级进度条，且跳过与网络传输重复统计的磁盘重建条
                # 和按文件数计数的 "Fetching N files" 条。
                unit = str(getattr(self, "unit", "B") or "B").strip().lower()
                desc = str(getattr(self, "desc", "") or "")
                if unit != "b" or desc.startswith("Reconstructing"):
                    return
                tracker._register(self)

            def update(self, amount: int = 1) -> None:
                if amount:
                    self._tracked_bytes += max(0, int(amount))
                    tracker._on_change()
                return super().update(amount)

            def close(self) -> None:
                tracker._on_change(force=True)
                super().close()

        return _TrackedBar

    @staticmethod
    def _format_size(num_bytes: int) -> str:
        size = float(num_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    @classmethod
    def _format_speed(cls, bytes_per_second: float) -> str:
        return f"{cls._format_size(int(bytes_per_second))}/s"









