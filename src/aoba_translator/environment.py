from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class EnvironmentReport:
    os: str
    os_version: str
    architecture: str
    python_version: str
    python_supported: bool
    cpu: str
    memory_gb: float | None
    gpu: str | None
    cuda_available: bool
    packages: dict[str, bool]
    commands: dict[str, bool]
    recommendations: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _memory_gb() -> float | None:
    if sys.platform == "win32":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return round(status.total_physical / (1024**3), 1)
        except (AttributeError, OSError, ValueError):
            return None

    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
        return round(page_size * page_count / (1024**3), 1)
    except (AttributeError, OSError, ValueError):
        return None


def _gpu_info() -> tuple[str | None, bool]:
    if importlib.util.find_spec("torch"):
        try:
            import torch

            if torch.cuda.is_available():
                return torch.cuda.get_device_name(0), True
        except Exception:
            pass

    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            completed = subprocess.run(
                [
                    nvidia_smi,
                    "--query-gpu=name",
                    "--format=csv,noheader",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            name = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else None
            return name, bool(name)
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    return None, False


def inspect_environment() -> EnvironmentReport:
    package_names = (
        "cv2",
        "easyocr",
        "huggingface_hub",
        "numpy",
        "PIL",
        "sentencepiece",
        "torch",
        "transformers",
    )
    packages = {name: importlib.util.find_spec(name) is not None for name in package_names}
    commands = {name: shutil.which(name) is not None for name in ("ollama", "7z", "unrar")}
    gpu, cuda = _gpu_info()
    supported = (3, 10) <= sys.version_info[:2] <= (3, 12)
    memory = _memory_gb()
    recommendations: list[str] = []
    if not supported:
        recommendations.append("推荐使用 Python 3.11；当前版本可能无法安装 OCR 或 PyTorch。")
    if not all(packages[name] for name in ("easyocr", "transformers", "torch", "cv2", "PIL")):
        recommendations.append("机器学习依赖未完整安装：初始化模型时会自动用 pip 补装，无需手动执行脚本。")
    if memory is not None and memory < 8:
        recommendations.append("内存少于 8 GB，建议减小翻译批次并避免一次处理大型漫画。")
    if not cuda:
        recommendations.append("未检测到 CUDA，将使用 CPU；功能不受影响，但漫画处理速度较慢。")

    return EnvironmentReport(
        os=platform.system(),
        os_version=platform.version(),
        architecture=platform.machine(),
        python_version=platform.python_version(),
        python_supported=supported,
        cpu=platform.processor() or platform.machine(),
        memory_gb=memory,
        gpu=gpu,
        cuda_available=cuda,
        packages=packages,
        commands=commands,
        recommendations=recommendations,
    )


def write_environment_report(path: Path) -> EnvironmentReport:
    report = inspect_environment()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


