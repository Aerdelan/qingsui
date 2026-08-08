from __future__ import annotations

import re
import shutil
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath


ARCHIVE_EXTENSIONS = {".zip", ".cbz", ".7z", ".rar"}
TEXT_EXTENSIONS = {".txt", ".md"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
IGNORED_NAMES = {"thumbs.db", ".ds_store"}


class ArchiveError(RuntimeError):
    pass


def _safe_destination(root: Path, member_name: str) -> Path:
    normalized = member_name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts:
        raise ArchiveError(f"压缩包包含不安全路径：{member_name}")
    destination = (root / Path(*pure.parts)).resolve()
    root_resolved = root.resolve()
    if destination != root_resolved and root_resolved not in destination.parents:
        raise ArchiveError(f"压缩包路径越界：{member_name}")
    return destination


def _validate_limits(
    entries: Iterable[tuple[str, int]], max_files: int, max_total_bytes: int
) -> list[tuple[str, int]]:
    checked: list[tuple[str, int]] = []
    total = 0
    for name, size in entries:
        checked.append((name, size))
        total += max(0, size)
        if len(checked) > max_files:
            raise ArchiveError(f"压缩包文件数超过限制（{max_files}）。")
        if total > max_total_bytes:
            raise ArchiveError("压缩包解压后体积超过安全限制。")
    return checked


def _extract_zip(source: Path, destination: Path, max_files: int, max_bytes: int) -> None:
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        _validate_limits(((item.filename, item.file_size) for item in infos), max_files, max_bytes)
        for item in infos:
            target = _safe_destination(destination, item.filename)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source_stream, target.open("wb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream)


def _extract_7z(source: Path, destination: Path, max_files: int, max_bytes: int) -> None:
    try:
        import py7zr
    except ImportError as exc:
        raise ArchiveError("处理 7z 需要安装 py7zr；请重新运行 start.ps1。") from exc

    with py7zr.SevenZipFile(source, mode="r") as archive:
        entries = archive.list()
        _validate_limits(
            ((item.filename, int(getattr(item, "uncompressed", 0) or 0)) for item in entries),
            max_files,
            max_bytes,
        )
        for item in entries:
            _safe_destination(destination, item.filename)
        archive.extractall(path=destination)


def _extract_rar(source: Path, destination: Path, max_files: int, max_bytes: int) -> None:
    try:
        import rarfile
    except ImportError as exc:
        raise ArchiveError("处理 RAR 需要安装 rarfile 和 unrar/7-Zip。") from exc

    with rarfile.RarFile(source) as archive:
        infos = archive.infolist()
        _validate_limits(((item.filename, item.file_size) for item in infos), max_files, max_bytes)
        for item in infos:
            target = _safe_destination(destination, item.filename)
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source_stream, target.open("wb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream)


def extract_archive(
    source: Path,
    destination: Path,
    *,
    max_files: int = 5000,
    max_total_bytes: int = 4 * 1024**3,
) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    extension = source.suffix.lower()
    if extension in {".zip", ".cbz"}:
        _extract_zip(source, destination, max_files, max_total_bytes)
    elif extension == ".7z":
        _extract_7z(source, destination, max_files, max_total_bytes)
    elif extension == ".rar":
        _extract_rar(source, destination, max_files, max_total_bytes)
    else:
        raise ArchiveError(f"不支持的压缩格式：{extension or '未知'}")
    return collect_content_files(destination)


def _natural_path_key(path: Path, root: Path) -> list[tuple[int, str | int]]:
    parts = re.split(r"(\d+)", path.relative_to(root).as_posix().lower())
    return [(1, int(part)) if part.isdigit() else (0, part) for part in parts]


def collect_content_files(root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.name.lower() not in IGNORED_NAMES
        ),
        key=lambda path: _natural_path_key(path, root),
    )


def classify_files(files: Iterable[Path]) -> str:
    paths = list(files)
    image_count = sum(path.suffix.lower() in IMAGE_EXTENSIONS for path in paths)
    text_count = sum(path.suffix.lower() in TEXT_EXTENSIONS for path in paths)
    if image_count and image_count >= text_count:
        return "manga"
    if text_count:
        return "novel"
    raise ArchiveError("未找到可处理的图片或 TXT/Markdown 文档。")


def classify_input(path: Path) -> str:
    extension = path.suffix.lower()
    if extension in TEXT_EXTENSIONS:
        return "novel"
    if extension in IMAGE_EXTENSIONS:
        return "manga"
    if extension in ARCHIVE_EXTENSIONS:
        return "archive"
    raise ArchiveError(f"不支持的文件类型：{extension or '未知'}")


def create_output_archive(source_dir: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    with zipfile.ZipFile(temporary, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in collect_content_files(source_dir):
            archive.write(path, path.relative_to(source_dir).as_posix())
    temporary.replace(destination)
    return destination


