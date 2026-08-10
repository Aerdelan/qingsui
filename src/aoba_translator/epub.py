"""EPUB 轻小说翻译：按 OPF 阅读顺序逐章翻译 XHTML 正文，图片与样式原样保留。"""

from __future__ import annotations

import html
import re
import zipfile
from collections.abc import Callable
from pathlib import Path

from .novel import JAPANESE_PATTERN, split_long_text
from .translation import Translator


BLOCK_PATTERN = re.compile(
    r"<(p|h1|h2|h3|h4|h5|h6|blockquote)(\s[^>]*)?>(.*?)</\1>",
    re.IGNORECASE | re.DOTALL,
)
BLOCK_TAG_COUNT = re.compile(
    r"<(p|h1|h2|h3|h4|h5|h6|blockquote)(\s[^>]*)?>|</(p|h1|h2|h3|h4|h5|h6|blockquote)>",
    re.IGNORECASE,
)
RT_PATTERN = re.compile(r"<rt[^>]*>.*?</rt>", re.IGNORECASE | re.DOTALL)
RUBY_CLOSE = re.compile(r"</ruby>", re.IGNORECASE)
TAG_PATTERN = re.compile(r"<[^>]+>")
SELF_CLOSING_CONTENT = re.compile(r"<(?:img|image|svg)\b", re.IGNORECASE)


def _reading_order(archive: zipfile.ZipFile) -> list[str]:
    """从 container.xml + OPF 解析 spine 阅读顺序，失败时退回文件名字典序。"""
    names = set(archive.namelist())
    opf_name = "content.opf"
    if "META-INF/container.xml" in names:
        container = archive.read("META-INF/container.xml").decode("utf-8", errors="replace")
        match = re.search(r'full-path="([^"]+)"', container)
        if match:
            opf_name = match.group(1)
    if opf_name not in names:
        return sorted(name for name in names if name.lower().endswith((".html", ".xhtml")))

    opf = archive.read(opf_name).decode("utf-8", errors="replace")
    base = Path(opf_name).parent
    href_by_id: dict[str, str] = {}
    for item_match in re.finditer(r"<item\b[^>]*>", opf):
        tag = item_match.group(0)
        item_id = re.search(r'\bid="([^"]+)"', tag)
        href = re.search(r'\bhref="([^"]+)"', tag)
        if item_id and href:
            href_by_id[item_id.group(1)] = href.group(1)
    spine_ids = re.findall(r"<itemref\s+idref=\"([^\"]+)\"", opf)
    order: list[str] = []
    for item_id in spine_ids:
        href = href_by_id.get(item_id)
        if not href:
            continue
        full = (base / href).as_posix() if str(base) != "." else href
        full = full.lstrip("/")
        if full in names:
            order.append(full)
    if not order:
        return sorted(name for name in names if name.lower().endswith((".html", ".xhtml")))
    return order


def _inner_text(inner: str) -> str:
    """提取块级元素的纯文本：去掉注音 rt 内容后剥离所有标签。"""
    text = RT_PATTERN.sub("", inner)
    text = RUBY_CLOSE.sub("", text)
    text = TAG_PATTERN.sub("", text)
    return html.unescape(text).strip()


def _collect_spans(
    content: str, region_start: int, region_end: int, units: list[str], spans: list[dict], max_chars: int
) -> None:
    for match in BLOCK_PATTERN.finditer(content, region_start, region_end):
        inner = match.group(3)
        if SELF_CLOSING_CONTENT.search(inner):
            continue
        # 内部还套着块级元素时，优先翻译内层段落，保留外层结构
        if BLOCK_TAG_COUNT.search(inner):
            _collect_spans(content, match.start(3), match.end(3), units, spans, max_chars)
            continue
        text = _inner_text(inner)
        if not text or not JAPANESE_PATTERN.search(text):
            continue
        indexes: list[int] = []
        for chunk in split_long_text(text, max_chars):
            indexes.append(len(units))
            units.append(chunk)
        spans.append({"start": match.start(), "end": match.end(), "units": indexes})


def _build_document_units(content: str, max_chars: int) -> tuple[list[str], list[dict]]:
    """把单个 XHTML 文档拆成翻译单元。

    返回 (units, spans)：spans 记录每个待替换块级元素的区间与对应单元下标。
    外层块内嵌块级元素时递归扫描内层，避免重复翻译。
    """
    units: list[str] = []
    spans: list[dict] = []
    _collect_spans(content, 0, len(content), units, spans, max_chars)
    return units, spans


def _rebuild_document(content: str, spans: list[dict], translations: list[str]) -> str:
    pieces: list[str] = []
    cursor = 0
    for span in spans:
        pieces.append(content[cursor : span["start"]])
        translated = "".join(translations[index] for index in span["units"])
        match = BLOCK_PATTERN.match(content, span["start"], span["end"])
        open_tag = match.group(0)[: match.group(0).index(">") + 1] if match else "<p>"
        close_tag = f"</{match.group(1)}>" if match else "</p>"
        pieces.append(f"{open_tag}{html.escape(translated, quote=False)}{close_tag}")
        cursor = span["end"]
    pieces.append(content[cursor:])
    return "".join(pieces)


class EpubError(RuntimeError):
    pass


def translate_epub(
    source: Path,
    destination: Path,
    translator: Translator,
    *,
    max_chars: int = 420,
    batch_size: int = 8,
    context_segments: int = 4,
    context_chars: int = 1800,
    progress: Callable[[int, str], None] | None = None,
) -> dict:
    """翻译整本 EPUB：收集全书单元、按上下文批量翻译、回写章节并重新打包。"""
    with zipfile.ZipFile(source) as archive:
        archive_names = archive.namelist()
        order = _reading_order(archive)
        chapters: list[dict] = []
        units: list[str] = []
        for name in order:
            content = archive.read(name).decode("utf-8", errors="replace")
            chapter_units, spans = _build_document_units(content, max_chars)
            if not chapter_units:
                continue
            chapters.append({"name": name, "content": content, "spans": spans, "start": len(units)})
            units.extend(chapter_units)

        if not units:
            raise EpubError("EPUB 中未找到日文正文。")

        translations: list[str] = []
        context_window: list[str] = []
        for start in range(0, len(units), batch_size):
            batch = units[start : start + batch_size]
            context = "\n".join(context_window)
            context = context[-context_chars:] if context_chars > 0 else ""
            batch_translations = translator.translate_batch(batch, context=context)
            translations.extend(batch_translations)
            if context_segments > 0:
                context_window = (context_window + batch_translations)[-context_segments:]
            if progress:
                done = min(start + len(batch), len(units))
                progress(done * 100 // len(units), f"正在翻译正文 {done}/{len(units)}")

        replacements: dict[str, bytes] = {}
        for index, chapter in enumerate(chapters):
            end = chapters[index + 1]["start"] if index + 1 < len(chapters) else len(translations)
            chapter_translations = translations[chapter["start"] : end]
            rebuilt = _rebuild_document(chapter["content"], chapter["spans"], chapter_translations)
            replacements[chapter["name"]] = rebuilt.encode("utf-8")

        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, mode="w") as output:
            for item in archive.infolist():
                if item.is_dir():
                    continue
                if item.filename == "mimetype":
                    info = zipfile.ZipInfo("mimetype", date_time=item.date_time)
                    output.writestr(info, archive.read(item), compress_type=zipfile.ZIP_STORED)
                elif item.filename in replacements:
                    output.writestr(item.filename, replacements[item.filename], compress_type=zipfile.ZIP_DEFLATED)
                else:
                    output.writestr(item, archive.read(item), compress_type=item.compress_type)

    return {
        "chapters": len(chapters),
        "segments": len(units),
        "images_kept": sum(
            1 for name in archive_names if name.lower().endswith((".jpeg", ".jpg", ".png", ".webp", ".gif"))
        ),
    }
