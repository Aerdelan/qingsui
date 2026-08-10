from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aoba_translator.assistant import AssistantError, PROJECT_SYSTEM_PROMPT, ProjectAssistant, _clip
from aoba_translator.archive import (
    ArchiveError,
    classify_files,
    classify_input,
    collect_content_files,
    extract_archive,
)
from aoba_translator.epub import EpubError, translate_epub
from aoba_translator.config import load_config
from aoba_translator.manga import merge_regions
from aoba_translator.messages import START_COMMAND, missing_dependencies_message
from aoba_translator.models import ModelManager
from aoba_translator.novel import build_translation_units, decode_text, restore_document, translate_novel
from aoba_translator.pipeline import TranslationPipeline
from aoba_translator.server import build_access_urls, sanitize_filename
from aoba_translator.translation import OllamaTranslator, clean_translation
from aoba_translator.domain import TextRegion


class ConfigTests(unittest.TestCase):
    def test_first_run_creates_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config(root)
            self.assertTrue(config.first_run)
            self.assertTrue(config.config_path.exists())
            loaded = json.loads(config.config_path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["app"]["port"], 8765)
            self.assertEqual(loaded["app"]["host"], "0.0.0.0")


class ConfigMigrationTests(unittest.TestCase):
    def test_legacy_marian_config_migrates_to_oral_ollama_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / ".local"
            local.mkdir()
            (local / "config.json").write_text(
                json.dumps(
                    {
                        "translation": {
                            "provider": "transformers",
                            "model_id": "shun89/opus-mt-ja-zh",
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(root)
            self.assertFalse(config.first_run)
            self.assertEqual(config.section("translation")["provider"], "ollama")
            self.assertEqual(config.section("translation")["style_profile"], "acgn_colloquial")
            self.assertEqual(config.section("translation")["ollama_model"], "qwen3.5:2b")


class ArchiveTests(unittest.TestCase):
    def test_zip_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "bad.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.txt", "blocked")
            with self.assertRaises(ArchiveError):
                extract_archive(archive_path, root / "output")

    def test_classifies_manga_and_novel(self) -> None:
        self.assertEqual(classify_files([Path("001.jpg"), Path("002.png")]), "manga")
        self.assertEqual(classify_files([Path("story.txt")]), "novel")


    def test_content_files_use_natural_page_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("10.jpg", "2.jpg", "1.jpg"):
                (root / name).write_bytes(b"page")
            self.assertEqual(
                [path.name for path in collect_content_files(root)],
                ["1.jpg", "2.jpg", "10.jpg"],
            )


class NovelTests(unittest.TestCase):
    def test_cp932_decode(self) -> None:
        text, encoding = decode_text("物語です。".encode("cp932"))
        self.assertEqual(text, "物語です。")
        self.assertEqual(encoding, "cp932")

    def test_layout_preserves_blank_lines(self) -> None:
        source = "第一話。\r\n\r\n第二話！\r\n"
        units, layout = build_translation_units(source, max_chars=20)
        translated = [f"译:{item}" for item in units]
        restored = restore_document(translated, layout)
        self.assertEqual(restored, "译:第一話。\r\n\r\n译:第二話！\r\n")

    def test_translation_passes_previous_segments_as_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "novel.txt"
            destination = root / "novel_zh.txt"
            source.write_bytes("第一話。\n第二話。\n第三話。\n".encode("utf-8"))
            translator = ContextTranslator()
            translate_novel(
                source,
                destination,
                translator,
                batch_size=1,
                context_segments=2,
            )
            self.assertEqual(translator.contexts[0], "")
            self.assertIn("中:第一話。", translator.contexts[1])
            self.assertIn("中:第二話。", translator.contexts[2])


class TranslationPromptTests(unittest.TestCase):
    def test_ollama_prompt_requests_colloquial_dialogue(self) -> None:
        translator = OllamaTranslator("murasaki")
        prompt = translator._system_prompt()
        self.assertIn("口语化", prompt)
        self.assertIn("不要机械直译", prompt)

    def test_clean_translation_removes_thinking_block(self) -> None:
        self.assertEqual(clean_translation("<think>分析</think>你好"), "你好")


class AssistantTests(unittest.TestCase):
    def test_prompt_contains_project_and_startup_knowledge(self) -> None:
        self.assertIn("rendering.py", PROJECT_SYSTEM_PROMPT)
        self.assertIn("powershell -ExecutionPolicy Bypass -File .\\start.ps1", PROJECT_SYSTEM_PROMPT)
        self.assertIn("不能执行 shell", PROJECT_SYSTEM_PROMPT)

    def test_context_values_are_clipped(self) -> None:
        clipped = _clip("青" * 20, 8)
        self.assertTrue(clipped.startswith("青" * 8))
        self.assertIn("已截断", clipped)

    def test_empty_question_is_rejected_before_model_call(self) -> None:
        assistant = ProjectAssistant.__new__(ProjectAssistant)
        with self.assertRaises(AssistantError):
            assistant.chat("   ")

class ContextTranslator:
    name = "context-test"

    def __init__(self) -> None:
        self.contexts: list[str] = []

    def translate_batch(self, texts, *, context: str = "") -> list[str]:
        self.contexts.append(context)
        return [f"中:{text}" for text in texts]


class MangaTests(unittest.TestCase):
    def test_adjacent_horizontal_lines_are_merged(self) -> None:
        regions = [
            TextRegion([(0, 0), (100, 0), (100, 20), (0, 20)], "今日は", 0.9),
            TextRegion([(5, 23), (95, 23), (95, 43), (5, 43)], "晴れ", 0.8),
        ]
        merged = merge_regions(regions)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].text, "今日は晴れ")


class PipelineTests(unittest.TestCase):
    def test_novel_pipeline_creates_final_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config(root)
            config.values["translation"]["provider"] = "echo"
            source = root / "cache_token_novel.txt"
            source.write_bytes("これは物語です。\n".encode("utf-8"))
            models = ModelManager(config)
            pipeline = TranslationPipeline(config, models)
            output, detected, details = pipeline.run(
                source, "test-job", display_name="novel.txt"
            )
            self.assertEqual(detected, "novel")
            self.assertEqual(details["count"], 1)
            self.assertTrue(output.exists())
            with zipfile.ZipFile(output) as archive:
                self.assertIn("novel_zh.txt", archive.namelist())
                translated = archive.read("novel_zh.txt").decode("utf-8-sig")
                self.assertEqual(translated, "これは物語です。\n")
                self.assertIn("translation-report.json", archive.namelist())


class EpubTests(unittest.TestCase):
    def _build_epub(self, path: Path) -> None:
        chapter1 = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ja"><head><title>ch1</title></head>'
            '<body><p class="calibre2"><ruby>物語<rt>ものがたり</rt></ruby>は始まる。</p>'
            '<p class="calibre2"><img src="../images/00001.jpeg"/></p>'
            '<p class="calibre2">English only paragraph.</p></body></html>'
        )
        chapter2 = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ja"><head><title>ch2</title></head>'
            '<body><blockquote><p>続きを書く。</p></blockquote></body></html>'
        )
        opf = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            "<manifest>"
            '<item id="c2" href="text/part0002.html" media-type="application/xhtml+xml"/>'
            '<item id="c1" href="text/part0001.html" media-type="application/xhtml+xml"/>'
            "</manifest>"
            '<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>'
        )
        container = (
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>'
        )
        with zipfile.ZipFile(path, mode="w") as archive:
            archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
            archive.writestr("META-INF/container.xml", container)
            archive.writestr("content.opf", opf)
            archive.writestr("text/part0001.html", chapter1)
            archive.writestr("text/part0002.html", chapter2)
            archive.writestr("images/00001.jpeg", b"\xff\xd8fake-image-bytes")

    def test_epub_translation_keeps_images_and_ruby_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.epub"
            self._build_epub(source)
            translator = ContextTranslator()
            destination = root / "book_zh.epub"
            metadata = translate_epub(source, destination, translator, batch_size=1)
            self.assertEqual(metadata["segments"], 2)
            self.assertEqual(metadata["chapters"], 2)
            self.assertEqual(metadata["images_kept"], 1)
            with zipfile.ZipFile(destination) as archive:
                names = archive.namelist()
                self.assertEqual(names[0], "mimetype")
                self.assertEqual(archive.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)
                chapter1 = archive.read("text/part0001.html").decode("utf-8")
                # ruby 注音被剥离后翻译，英文段落与图片段落保持原样
                self.assertIn("中:物語は始まる。", chapter1)
                self.assertNotIn("ものがたり", chapter1)
                self.assertIn("English only paragraph.", chapter1)
                self.assertIn('<img src="../images/00001.jpeg"/>', chapter1)
                chapter2 = archive.read("text/part0002.html").decode("utf-8")
                self.assertIn("中:続きを書く。", chapter2)
                self.assertIn("<blockquote>", chapter2)
                self.assertEqual(archive.read("images/00001.jpeg"), b"\xff\xd8fake-image-bytes")
            # 上下文跨章节传递
            self.assertTrue(any("中:物語は始まる。" in context for context in translator.contexts))

    def test_epub_without_japanese_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "empty.epub"
            with zipfile.ZipFile(source, mode="w") as archive:
                archive.writestr("mimetype", "application/epub+zip")
                archive.writestr(
                    "text/part0001.html",
                    '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Hello.</p></body></html>',
                )
            with self.assertRaises(EpubError):
                translate_epub(source, root / "out.epub", ContextTranslator())

    def test_classify_input_recognizes_epub(self) -> None:
        self.assertEqual(classify_input(Path("book.epub")), "epub")


class MessageTests(unittest.TestCase):
    def test_dependency_message_contains_copyable_command(self) -> None:
        message = missing_dependencies_message(["transformers", "cv2", "easyocr", "torch"])
        self.assertEqual(
            message,
            f"缺少运行依赖：cv2, easyocr, torch, transformers。请在项目目录执行：{START_COMMAND}",
        )


class ServerTests(unittest.TestCase):
    def test_filename_is_sanitized(self) -> None:
        self.assertEqual(sanitize_filename("..%2Fbook%3F.txt"), "book_.txt")

    def test_wildcard_host_builds_loopback_and_lan_urls(self) -> None:
        self.assertEqual(
            build_access_urls("0.0.0.0", 8765, ["192.168.1.20"]),
            ["http://127.0.0.1:8765", "http://192.168.1.20:8765"],
        )


if __name__ == "__main__":
    unittest.main()










