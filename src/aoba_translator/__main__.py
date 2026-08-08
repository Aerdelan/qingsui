from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import load_config
from .environment import inspect_environment
from .jobs import JobManager
from .models import ModelManager
from .pipeline import TranslationPipeline
from .server import serve


def _build_runtime():
    config = load_config()
    models = ModelManager(config)
    pipeline = TranslationPipeline(config, models)
    jobs = JobManager(config, models, pipeline)
    return config, models, pipeline, jobs


def _translate_file(path: Path) -> int:
    config, models, _, jobs = _build_runtime()
    if not models.status()["ready"]:
        print("模型尚未就绪，正在初始化...")
        models.prepare(lambda value, message: print(f"[{value:3d}%] {message}"))
    record = jobs.submit_translation(path.resolve(), path.name)
    while True:
        current = jobs.store.get(record.id)
        if not current:
            return 1
        print(f"\r[{current.progress:3d}%] {current.stage:30}", end="", flush=True)
        if current.status in {"completed", "failed"}:
            print()
            if current.status == "completed":
                print(f"输出：{current.output_path}")
                return 0
            print(f"失败：{current.message}", file=sys.stderr)
            return 1
        time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser(description="青穗翻译台")
    subparsers = parser.add_subparsers(dest="command")
    serve_parser = subparsers.add_parser("serve", help="启动本地 Web 界面")
    serve_parser.add_argument("--no-browser", action="store_true")
    serve_parser.add_argument("--skip-model-download", action="store_true")
    subparsers.add_parser("setup", help="下载并初始化本地模型")
    subparsers.add_parser("inspect", help="输出环境检测结果")
    translate_parser = subparsers.add_parser("translate", help="直接翻译文件")
    translate_parser.add_argument("path", type=Path)
    args = parser.parse_args()

    if args.command == "inspect":
        print(json.dumps(inspect_environment().to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "translate":
        if not args.path.exists():
            parser.error(f"文件不存在：{args.path}")
        return _translate_file(args.path)

    config, models, _, jobs = _build_runtime()
    if args.command == "setup":
        models.prepare(lambda value, message: print(f"[{value:3d}%] {message}"))
        return 0

    skip_download = bool(getattr(args, "skip_model_download", False))
    if not skip_download and config.section("bootstrap").get("auto_download_models", True):
        if not models.status()["ready"]:
            setup_job = jobs.submit_setup()
            print(f"首次运行：已创建模型初始化任务 {setup_job.id[:8]}")
    serve(
        config,
        jobs,
        models,
        open_browser=not bool(getattr(args, "no_browser", False)),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
