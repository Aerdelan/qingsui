from __future__ import annotations

from collections.abc import Iterable


START_COMMAND = r"powershell -ExecutionPolicy Bypass -File .\start.ps1"


def start_command_hint(prefix: str = "请在项目目录执行") -> str:
    return f"{prefix}：{START_COMMAND}"


def missing_dependencies_message(names: Iterable[str]) -> str:
    dependencies = ", ".join(sorted(set(names)))
    return f"缺少运行依赖：{dependencies}。{start_command_hint()}"
