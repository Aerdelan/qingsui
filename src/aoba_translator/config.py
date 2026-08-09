from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "app": {"host": "0.0.0.0", "port": 8765, "open_browser": True},
    "bootstrap": {"auto_download_models": True},
    "translation": {
        "provider": "ollama",
        "model_id": "shun89/opus-mt-ja-zh",
        "ollama_model": "qwen3.5:2b",
        "ollama_base_url": "http://127.0.0.1:11434",
        "style_profile": "acgn_colloquial",
        "temperature": 0.25,
        "context_chars": 1800,
        "context_segments": 4,
        "batch_size": 8,
        "max_input_chars": 420,
        "target_language": "简体中文",
    },
    "ocr": {
        "provider": "manga",
        "vision_model": "glm-ocr",
        "ollama_base_url": "http://127.0.0.1:11434",
        "vision_timeout": 300,
        "languages": ["ja", "en"],
        "gpu": "auto",
        "min_confidence": 0.1,
        "text_threshold": 0.5,
        "low_text": 0.3,
        "link_threshold": 0.3,
        "canvas_size": 2560,
        "min_region_size": 8,
    },
    "rendering": {
        "font_path": None,
        "font_min_size": 12,
        "font_max_size": 72,
        "mask_padding": 3,
        "inpaint_radius": 3,
    },
    "limits": {
        "max_upload_mb": 1024,
        "max_archive_files": 5000,
        "max_extracted_mb": 4096,
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def discover_project_root() -> Path:
    configured = os.environ.get("AOBA_HOME")
    if configured:
        return Path(configured).expanduser().resolve()

    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        pyproject = candidate / "pyproject.toml"
        if pyproject.exists():
            try:
                if "aoba-translator" in pyproject.read_text(encoding="utf-8-sig"):
                    return candidate
            except OSError:
                continue
    return current


@dataclass(slots=True)
class AppConfig:
    root: Path
    values: dict[str, Any]
    first_run: bool

    @property
    def local_dir(self) -> Path:
        return self.root / ".local"

    @property
    def config_path(self) -> Path:
        return self.local_dir / "config.json"

    @property
    def runtime_path(self) -> Path:
        return self.local_dir / "runtime.json"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"

    @property
    def models_dir(self) -> Path:
        return self.root / "models"

    def section(self, name: str) -> dict[str, Any]:
        value = self.values.get(name, {})
        return value if isinstance(value, dict) else {}

    def ensure_directories(self) -> None:
        for path in (
            self.local_dir,
            self.upload_dir,
            self.work_dir,
            self.output_dir,
            self.models_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def save(self) -> None:
        self.ensure_directories()
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.values, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.config_path)
        self.first_run = False

    def load_runtime(self) -> dict[str, Any]:
        if not self.runtime_path.exists():
            return {}
        try:
            return json.loads(self.runtime_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save_runtime(self, values: dict[str, Any]) -> None:
        self.ensure_directories()
        temporary = self.runtime_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.runtime_path)


LEGACY_TRANSLATION_MODELS = {
    "Helsinki-NLP/opus-mt-ja-zh",
    "shun89/opus-mt-ja-zh",
}


def _migrate_config(values: dict[str, Any]) -> bool:
    changed = False
    translation = values.get("translation")
    if isinstance(translation, dict):
        provider = str(translation.get("provider", "")).lower()
        model_id = str(translation.get("model_id", ""))
        if provider == "transformers" and model_id in LEGACY_TRANSLATION_MODELS:
            translation.update(
                {
                    "provider": "ollama",
                    "ollama_model": "qwen3.5:2b",
                    "ollama_base_url": "http://127.0.0.1:11434",
                    "style_profile": "acgn_colloquial",
                    "temperature": 0.25,
                    "context_chars": 1800,
                    "context_segments": 4,
                }
            )
            changed = True
    # 纯 EasyOCR 识别对漫画装饰字体漏检严重，统一迁移到混合漫画 OCR 引擎。
    ocr = values.get("ocr")
    if isinstance(ocr, dict) and str(ocr.get("provider", "")).lower() == "easyocr":
        ocr["provider"] = "manga"
        changed = True
    return changed


def load_config(root: Path | None = None) -> AppConfig:
    resolved_root = (root or discover_project_root()).resolve()
    config_path = resolved_root / ".local" / "config.json"
    first_run = not config_path.exists()
    override: dict[str, Any] = {}
    migrated = False
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                override = loaded
                migrated = _migrate_config(override)
        except (OSError, json.JSONDecodeError):
            override = {}

    config = AppConfig(resolved_root, _merge(DEFAULT_CONFIG, override), first_run)
    config.ensure_directories()
    if first_run or migrated:
        config.save()
        config.first_run = first_run
    return config









