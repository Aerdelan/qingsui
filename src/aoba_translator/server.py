from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import threading
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .assistant import AssistantError, ProjectAssistant
from .config import AppConfig
from .environment import inspect_environment
from .jobs import JobManager
from .models import ModelManager


SAFE_FILENAME = re.compile(r"[^\w.\-()\[\] ]+", re.UNICODE)


def sanitize_filename(value: str) -> str:
    decoded = urllib.parse.unquote(value).replace("\\", "/").split("/")[-1].strip()
    cleaned = SAFE_FILENAME.sub("_", decoded).strip(" .")
    return cleaned[:180] or "upload.bin"


def discover_local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = item[4][0]
            if not address.startswith(("127.", "169.254.", "0.")):
                addresses.add(address)
    except OSError:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            address = probe.getsockname()[0]
            if not address.startswith(("127.", "169.254.", "0.")):
                addresses.add(address)
    except OSError:
        pass
    return sorted(addresses)


def build_access_urls(
    host: str, port: int, local_addresses: list[str] | None = None
) -> list[str]:
    if host in {"", "0.0.0.0", "::"}:
        addresses = ["127.0.0.1", *(local_addresses or discover_local_ipv4_addresses())]
    else:
        addresses = [host]
    unique_addresses = list(dict.fromkeys(addresses))
    return [f"http://{address}:{port}" for address in unique_addresses]


class AobaHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        config: AppConfig,
        jobs: JobManager,
        models: ModelManager,
    ) -> None:
        super().__init__(address, handler_class)
        self.app_config = config
        self.jobs = jobs
        self.models = models
        self.assistant = ProjectAssistant(config, models, jobs)
        self.web_root = Path(__file__).parent / "web"


class RequestHandler(BaseHTTPRequestHandler):
    server: AobaHttpServer

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/system":
            self._json(
                {
                    "environment": inspect_environment().to_dict(),
                    "models": self.server.models.status(),
                    "config": self._public_config(),
                }
            )
            return
        if path == "/api/jobs":
            self._json({"jobs": [item.to_dict() for item in self.server.jobs.store.list()]})
            return
        if path == "/api/assistant/context":
            self._json(self.server.assistant.context_summary())
            return
        if path.startswith("/api/jobs/"):
            self._handle_job_get(path)
            return
        if path == "/api/outputs":
            outputs = sorted(
                self.server.app_config.output_dir.glob("*.zip"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )[:20]
            self._json(
                {
                    "outputs": [
                        {
                            "name": item.name,
                            "size": item.stat().st_size,
                            "modified": item.stat().st_mtime,
                        }
                        for item in outputs
                    ]
                }
            )
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/setup":
            record = self.server.jobs.submit_setup()
            self._json(record.to_dict(), status=HTTPStatus.ACCEPTED)
            return
        if parsed.path == "/api/jobs":
            self._handle_upload()
            return
        if parsed.path == "/api/assistant/chat":
            self._handle_assistant_chat()
            return
        self._json({"error": "接口不存在"}, status=HTTPStatus.NOT_FOUND)

    def _handle_assistant_chat(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0:
            self._json({"error": "对话内容为空"}, status=HTTPStatus.BAD_REQUEST)
            return
        if content_length > 512 * 1024:
            self._json({"error": "对话内容超过 512 KB 限制"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json({"error": "请求 JSON 格式无效"}, status=HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(payload, dict):
            self._json({"error": "请求内容必须是 JSON 对象"}, status=HTTPStatus.BAD_REQUEST)
            return
        conversation = payload.get("conversation")
        if not isinstance(conversation, list):
            conversation = []
        try:
            result = self.server.assistant.chat(
                str(payload.get("message") or ""),
                conversation,
            )
        except AssistantError as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._json(result)

    def _handle_upload(self) -> None:
        filename_header = self.headers.get("X-Filename")
        if not filename_header:
            self._json({"error": "缺少 X-Filename 请求头"}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        maximum = int(self.server.app_config.section("limits").get("max_upload_mb", 1024)) * 1024**2
        if content_length <= 0:
            self._json({"error": "上传内容为空"}, status=HTTPStatus.BAD_REQUEST)
            return
        if content_length > maximum:
            self._json({"error": "文件超过上传大小限制"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return

        filename = sanitize_filename(filename_header)
        upload_id = os.urandom(8).hex()
        upload_path = self.server.app_config.upload_dir / f"{upload_id}_{filename}"
        remaining = content_length
        with upload_path.open("wb") as target:
            while remaining > 0:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                target.write(chunk)
                remaining -= len(chunk)
        if remaining:
            upload_path.unlink(missing_ok=True)
            self._json({"error": "上传连接提前中断"}, status=HTTPStatus.BAD_REQUEST)
            return
        record = self.server.jobs.submit_translation(upload_path, filename)
        self._json(record.to_dict(), status=HTTPStatus.ACCEPTED)

    def _handle_job_get(self, path: str) -> None:
        parts = [part for part in path.split("/") if part]
        if len(parts) < 3:
            self._json({"error": "任务不存在"}, status=HTTPStatus.NOT_FOUND)
            return
        job_id = parts[2]
        record = self.server.jobs.store.get(job_id)
        if not record:
            self._json({"error": "任务不存在"}, status=HTTPStatus.NOT_FOUND)
            return
        if len(parts) == 4 and parts[3] == "download":
            if record.status != "completed" or not record.output_path or not record.output_path.exists():
                self._json({"error": "产物尚不可下载"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_file(record.output_path, download=True)
            return
        self._json(record.to_dict())

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        candidate = (self.server.web_root / relative).resolve()
        web_root = self.server.web_root.resolve()
        if candidate != web_root and web_root not in candidate.parents:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.exists() or not candidate.is_file():
            candidate = web_root / "index.html"
        self._send_file(candidate)

    def _send_file(self, path: Path, download: bool = False) -> None:
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-store" if download else "no-cache")
        if download:
            encoded = urllib.parse.quote(path.name)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded}")
        self.end_headers()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                self.wfile.write(chunk)

    def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _public_config(self) -> dict[str, Any]:
        config = self.server.app_config
        app = config.section("app")
        host = str(app.get("host", "0.0.0.0"))
        port = int(app.get("port", 8765))
        return {
            "first_run": config.first_run,
            "output_dir": str(config.output_dir),
            "listen_host": host,
            "access_urls": build_access_urls(host, port),
            "translation": config.section("translation"),
            "ocr": config.section("ocr"),
            "limits": config.section("limits"),
        }

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} - {format_string % args}")


def serve(
    config: AppConfig,
    jobs: JobManager,
    models: ModelManager,
    *,
    open_browser: bool | None = None,
) -> None:
    app = config.section("app")
    host = str(app.get("host", "0.0.0.0"))
    port = int(app.get("port", 8765))
    server = AobaHttpServer((host, port), RequestHandler, config, jobs, models)
    should_open = bool(app.get("open_browser", True)) if open_browser is None else open_browser
    urls = build_access_urls(host, port)
    print("青穗翻译台已启动，可通过以下地址访问：")
    for url in urls:
        print(f"  - {url}")
    if host in {"0.0.0.0", "::"}:
        print("如其他设备无法访问，请允许 Windows 防火墙中的 Python 专用网络通信。")
    if should_open:
        threading.Timer(0.8, lambda: webbrowser.open(urls[0])).start()
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\n正在关闭服务...")
    finally:
        server.server_close()
        jobs.shutdown()



