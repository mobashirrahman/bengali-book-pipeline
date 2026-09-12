"""Loopback-only manual OCR review website.

Start with ``python -m pdf_craft_tool.review_server --cluster-root
pdf-craft-output/cluster``. The server is deliberately a foreground local tool;
it does not install a service or accept browser-supplied filesystem paths.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from pdf_craft.document import PDFCraftExtraction
from pdf_craft.error import PDFError
from pdf_craft.pdf.handler import DefaultPDFHandler

from .review_dataset import DEFAULT_CAP, ReviewDataset, load_or_create_manifest
from .review_store import ReviewStore, RevisionConflict, UnknownEntry

MAX_REQUEST_BYTES = 2_000_000


class CropUnavailable(RuntimeError):
    """A review entry has no readable original image source."""


class ReviewApp:
    def __init__(
        self,
        dataset: ReviewDataset,
        review_db: Path,
        *,
        cache_root: Path | None = None,
        pdf_handler=None,
        extraction_opener=None,
    ):
        self.dataset = dataset
        self.store = ReviewStore(review_db, dataset.entries)
        self.token = secrets.token_urlsafe(24)
        self.cache_root = Path(cache_root or dataset.path.parent / "cache")
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.pdf_handler = pdf_handler or DefaultPDFHandler()
        self.extraction_opener = extraction_opener or PDFCraftExtraction.open

    def close(self) -> None:
        self.store.close()

    def session(self) -> dict:
        return {
            "token": self.token,
            "progress": self.store.progress(),
            "next": self.store.next_unreviewed(),
        }

    def entry(self, entry_id: str) -> dict:
        entry = self.store.get(entry_id)
        if entry is None:
            raise UnknownEntry(entry_id)
        return entry

    def crop(self, entry_id: str) -> bytes:
        entry = self.entry(entry_id)
        if not entry.get("source_exists") or not entry.get("source"):
            raise CropUnavailable("Original PDF is unavailable")
        page, bbox = entry.get("page"), entry.get("bbox")
        if (
            not isinstance(page, int)
            or page < 1
            or not isinstance(bbox, list)
            or len(bbox) != 4
        ):
            raise CropUnavailable("This audit record has no usable page geometry")
        source = Path(entry["source"])
        expected_hash = entry.get("source_sha256")
        if isinstance(expected_hash, str) and expected_hash:
            try:
                with source.open("rb") as stream:
                    actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            except OSError as error:
                raise CropUnavailable(f"Original PDF cannot be read: {error}") from error
            if not hmac.compare_digest(actual_hash, expected_hash):
                raise CropUnavailable("Original PDF does not match the frozen audit source")
        cache_suffix = expected_hash[:16] if isinstance(expected_hash, str) and expected_hash else "unhashed"
        target = self.cache_root / f"{entry_id}-{cache_suffix}.png"
        if target.is_file():
            return target.read_bytes()
        extraction_value = entry.get("extraction")
        dpi = 300
        if isinstance(extraction_value, str) and extraction_value:
            try:
                extraction = self.extraction_opener(extraction_value)
                dpi = extraction.render_dpi()
            except (OSError, ValueError, RuntimeError):
                # The audit is still reviewable as text when an old extraction was pruned.
                dpi = 300
        document = self.pdf_handler.open(source)
        image = None
        try:
            image = document.render_page(page, dpi)
            left, top, right, bottom = bbox
            crop = image.crop(
                (
                    max(0, left - 12),
                    max(0, top - 12),
                    min(image.width, right + 12),
                    min(image.height, bottom + 12),
                )
            )
            crop.thumbnail((1536, 1536), Image.Resampling.LANCZOS)
            stream = io.BytesIO()
            crop.save(stream, format="PNG", optimize=True)
            payload = stream.getvalue()
        except (OSError, ValueError, RuntimeError, PDFError) as error:
            raise CropUnavailable(str(error)) from error
        finally:
            if image is not None:
                image.close()
            document.close()
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(payload)
        temporary.replace(target)
        return payload


class ReviewHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, app: ReviewApp):
        super().__init__(address, ReviewRequestHandler)
        self.app = app

    def server_close(self):
        super().server_close()
        self.app.close()


class ReviewRequestHandler(BaseHTTPRequestHandler):
    server: ReviewHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    @property
    def app(self) -> ReviewApp:
        return self.server.app

    def do_GET(self):
        route = urlsplit(self.path)
        try:
            if route.path == "/":
                return self._static("index.html", "text/html; charset=utf-8")
            if route.path == "/static/styles.css":
                return self._static("styles.css", "text/css; charset=utf-8")
            if route.path == "/static/app.js":
                return self._static("app.js", "text/javascript; charset=utf-8")
            if route.path == "/api/session":
                return self._json(self.app.session())
            if route.path == "/api/progress":
                return self._json(self.app.store.progress())
            if route.path == "/api/export":
                kind = parse_qs(route.query).get("kind", ["verified"])[0]
                if kind not in ("verified", "all"):
                    return self._error(400, "kind must be verified or all")
                rows = self.app.store.export_rows(verified_only=kind == "verified")
                body = "".join(_stable_json(row) + "\n" for row in rows).encode("utf-8")
                return self._send(
                    200,
                    body,
                    "application/x-ndjson; charset=utf-8",
                    {
                        "Content-Disposition": f'attachment; filename="ocr-review-{kind}.jsonl"'
                    },
                )
            parts = route.path.strip("/").split("/")
            if len(parts) == 3 and parts[0:2] == ["api", "entry"] and parts[2]:
                return self._json(self.app.entry(parts[2]))
            if (
                len(parts) == 4
                and parts[0:2] == ["api", "entry"]
                and parts[3] == "crop"
            ):
                payload = self.app.crop(parts[2])
                return self._send(200, payload, "image/png")
            return self._error(404, "Not found")
        except UnknownEntry:
            return self._error(404, "Unknown review entry")
        except CropUnavailable as error:
            return self._error(404, str(error))
        except (OSError, ValueError, RuntimeError, PDFError) as error:
            return self._error(500, str(error))

    def do_POST(self):
        route = urlsplit(self.path)
        parts = route.path.strip("/").split("/")
        if not (
            (len(parts) == 4 and parts[:2] == ["api", "entry"] and parts[3] == "review")
            or (len(parts) == 3 and parts[:2] == ["api", "review"])
        ):
            return self._error(404, "Not found")
        if not self._mutation_allowed():
            return self._error(403, "Missing or invalid review token/origin")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_REQUEST_BYTES:
            return self._error(413, "Request is too large")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("JSON body must be an object")
            required = {"verified_text", "decision", "revision"}
            if not required <= payload.keys():
                raise ValueError("verified_text, decision, and revision are required")
            entry = self.app.store.save(
                parts[2],
                verified_text=payload["verified_text"],
                decision=payload["decision"],
                revision=payload["revision"],
                accepted_edits=payload.get("accepted_edits", []),
            )
            return self._json(
                {"entry": entry, "progress": self.app.store.progress()}, 200
            )
        except RevisionConflict as error:
            return self._error(409, str(error))
        except UnknownEntry:
            return self._error(404, "Unknown review entry")
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as error:
            return self._error(400, str(error))

    def _mutation_allowed(self) -> bool:
        supplied = self.headers.get("X-Review-Token", "")
        if not hmac.compare_digest(supplied, self.app.token):
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = urlsplit(origin).hostname
        return host in {"127.0.0.1", "localhost", "::1"}

    def _static(self, filename: str, content_type: str):
        path = Path(__file__).parent / "review_static" / filename
        return self._send(200, path.read_bytes(), content_type)

    def _json(self, payload, status=200):
        return self._send(
            status,
            (_stable_json(payload) + "\n").encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(self, status: int, message: str):
        return self._json({"error": message}, status)

    def _send(
        self, status: int, body: bytes, content_type: str, extra: dict | None = None
    ):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def _stable_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_app(args: argparse.Namespace) -> ReviewApp:
    cluster_root = args.cluster_root.resolve()
    manifest = load_or_create_manifest(
        cluster_root, manifest_path=args.manifest, queue_path=args.queue, cap=args.cap
    )
    review_root = args.review_root or manifest.path.parent
    return ReviewApp(
        manifest,
        args.review_db or review_root / "review.sqlite3",
        cache_root=review_root / "cache",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cluster-root",
        type=Path,
        default=Path("pdf-craft-output/cluster"),
        help="completed local cluster jobs (default: pdf-craft-output/cluster)",
    )
    parser.add_argument("--queue", type=Path, help="read-only queue.sqlite3 override")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="frozen manifest (default: CLUSTER/review/manifest.json)",
    )
    parser.add_argument(
        "--review-db",
        type=Path,
        help="separate review SQLite DB (default: CLUSTER/review/review.sqlite3)",
    )
    parser.add_argument(
        "--review-root",
        type=Path,
        help="cache/export directory (default: manifest directory)",
    )
    parser.add_argument(
        "--cap",
        type=int,
        default=DEFAULT_CAP,
        help="maximum frozen entries (default: 1000)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="bind address; loopback is recommended"
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="TCP port (default: 8765)"
    )
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("The review website must bind to loopback")
    app = build_app(args)
    server = ReviewHTTPServer((args.host, args.port), app)
    print(f"OCR review running at http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
