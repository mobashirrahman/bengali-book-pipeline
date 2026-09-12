"""Loopback-only line-level gold adjudication website.

Build tasks from one finished ``book`` work directory, then serve cards::

    python -m pdf_craft_tool.gold_server --book-root pdf-craft-output/sarat-10

The first launch freezes the manifest in ``<book-root>/gold/manifest.json``
and resumes labels in the separate ``gold.sqlite3``. Later launches reuse the
frozen manifest so IDs stay stable for benchmarking. Nothing here trains a
model or calls one; it only collects human-verified line text.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from pdf_craft.error import PDFError
from pdf_craft.pdf.handler import DefaultPDFHandler

from .gold_store import (
    VERIFIED_DECISIONS,
    GoldStore,
    RevisionConflict,
    UnknownEntry,
)
from .gold_tasks import DEFAULT_CAP, GoldDataset, load_or_create_manifest

MAX_REQUEST_BYTES = 2_000_000
CROP_PAD = 12
CROP_MAX = 1600


class CropUnavailable(RuntimeError):
    """A gold entry has no readable original image source."""


class GoldApp:
    def __init__(
        self,
        dataset: GoldDataset,
        review_db: Path,
        *,
        source: Path | None = None,
        cache_root: Path | None = None,
        pdf_handler=None,
    ):
        self.dataset = dataset
        self.store = GoldStore(review_db, dataset.entries)
        self.token = secrets.token_urlsafe(24)
        self.cache_root = Path(cache_root or dataset.path.parent / "cache")
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.pdf_handler = pdf_handler or DefaultPDFHandler()
        self.source_override = Path(source) if source is not None else None
        self.document = None
        self.page_index: int | None = None
        self.image = None
        self.source_path: Path | None = None
        self._crop_lock = threading.RLock()

    def close(self) -> None:
        with self._crop_lock:
            self.store.close()
            if self.image is not None:
                try:
                    self.image.close()
                except (OSError, ValueError):
                    pass
            if self.document is not None:
                try:
                    self.document.close()
                except (OSError, ValueError, RuntimeError):
                    pass

    def session(self) -> dict:
        stats = self.store.stats()
        return {"token": self.token, "progress": stats, "stats": stats,
                "next": self.store.next_unreviewed()}

    def entry(self, entry_id: str) -> dict:
        entry = self.store.get(entry_id)
        if entry is None:
            raise UnknownEntry(entry_id)
        return entry

    def _source_for(self, entry: dict) -> Path:
        if self.source_override is not None:
            return self.source_override
        value = entry.get("source", "")
        return Path(value) if isinstance(value, str) and value else Path()

    def crop(self, entry_id: str) -> bytes:
        """Render and cache one crop while serializing shared PDF state."""
        with self._crop_lock:
            return self._crop_locked(entry_id)

    def _crop_locked(self, entry_id: str) -> bytes:
        entry = self.entry(entry_id)
        source = self._source_for(entry)
        if not str(source) or not source.is_file():
            raise CropUnavailable("Original PDF is unavailable")
        page, bbox = entry.get("page"), entry.get("bbox")
        if (
            not isinstance(page, int)
            or page < 1
            or not isinstance(bbox, list)
            or len(bbox) != 4
        ):
            raise CropUnavailable("This line record has no usable page geometry")
        expected_hash = entry.get("source_sha256")
        if isinstance(expected_hash, str) and expected_hash:
            try:
                with source.open("rb") as stream:
                    actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            except OSError as error:
                raise CropUnavailable(f"Original PDF cannot be read: {error}") from error
            if not hmac.compare_digest(actual_hash, expected_hash):
                raise CropUnavailable("Original PDF does not match the frozen gold source")
        target = self.cache_root / f"{entry_id}.png"
        if target.is_file():
            return target.read_bytes()
        dpi = entry.get("dpi") if isinstance(entry.get("dpi"), int) else 300
        if self.document is None or self.source_path != source:
            self._reset_document()
            self.document = self.pdf_handler.open(source)
            self.source_path = source
        if self.page_index != page:
            if self.image is not None:
                try:
                    self.image.close()
                except (OSError, ValueError):
                    pass
            self.image = self.document.render_page(page, dpi)
            self.page_index = page
        assert self.image is not None
        left, top, right, bottom = (int(v) for v in bbox)
        crop = self.image.crop(
            (
                max(0, left - CROP_PAD),
                max(0, top - CROP_PAD),
                min(self.image.width, right + CROP_PAD),
                min(self.image.height, bottom + CROP_PAD),
            )
        )
        # Lines are short: upscale narrow crops so vowel signs stay readable.
        if crop.width < 1200:
            crop = crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.LANCZOS)
        crop.thumbnail((CROP_MAX, CROP_MAX), Image.Resampling.LANCZOS)
        stream = io.BytesIO()
        crop.save(stream, format="PNG", optimize=True)
        payload = stream.getvalue()
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(payload)
        temporary.replace(target)
        return payload

    def _reset_document(self) -> None:
        if self.image is not None:
            try:
                self.image.close()
            except (OSError, ValueError):
                pass
            self.image = None
        if self.document is not None:
            try:
                self.document.close()
            except (OSError, ValueError, RuntimeError):
                pass
            self.document = None
        self.page_index = None
        self.source_path = None


class GoldHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, app: GoldApp):
        super().__init__(address, GoldRequestHandler)
        self.app = app

    def server_close(self):
        super().server_close()
        self.app.close()


class GoldRequestHandler(BaseHTTPRequestHandler):
    server: GoldHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    @property
    def app(self) -> GoldApp:
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
            if route.path in ("/api/stats", "/api/progress"):
                return self._json(self.app.store.stats())
            if route.path == "/api/export":
                kind = parse_qs(route.query).get("kind", ["verified"])[0]
                if kind not in ("verified", "all"):
                    return self._error(400, "kind must be verified or all")
                rows = self.app.store.export_rows(verified_only=kind == "verified")
                body = "".join(_stable_json(row) + "\n" for row in rows).encode("utf-8")
                return self._send(
                    200, body, "application/x-ndjson; charset=utf-8",
                    {"Content-Disposition": f'attachment; filename="gold-{kind}.jsonl"'},
                )
            parts = route.path.strip("/").split("/")
            if len(parts) == 3 and parts[0:2] == ["api", "task"] and parts[2]:
                return self._json(self.app.entry(parts[2]))
            if len(parts) == 3 and parts[0:2] == ["api", "entry"] and parts[2]:
                return self._json(self.app.entry(parts[2]))
            if (
                len(parts) == 4
                and parts[0:2] == ["api", "task"]
                and parts[3] == "crop"
            ):
                return self._send(200, self.app.crop(parts[2]), "image/png")
            if (
                len(parts) == 4
                and parts[0:2] == ["api", "entry"]
                and parts[3] == "crop"
            ):
                return self._send(200, self.app.crop(parts[2]), "image/png")
            return self._error(404, "Not found")
        except UnknownEntry:
            return self._error(404, "Unknown gold entry")
        except CropUnavailable as error:
            return self._error(404, str(error))
        except (OSError, ValueError, RuntimeError, PDFError) as error:
            return self._error(500, str(error))

    def do_POST(self):
        route = urlsplit(self.path)
        parts = route.path.strip("/").split("/")
        if not (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] in ("task", "entry")
            and parts[3] == "review"
        ):
            return self._error(404, "Not found")
        if not self._mutation_allowed():
            return self._error(403, "Missing or invalid gold token/origin")
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
            if payload["decision"] in VERIFIED_DECISIONS:
                # A gold label is only accepted after the exact frozen source
                # has been available, hash-checked and rendered successfully.
                self.app.crop(parts[2])
            entry = self.app.store.save(
                parts[2],
                verified_text=payload["verified_text"],
                decision=payload["decision"],
                revision=payload["revision"],
                elapsed_ms=int(payload.get("elapsed_ms", 0)),
            )
            return self._json(
                {"entry": entry, "progress": self.app.store.stats()}, 200
            )
        except RevisionConflict as error:
            return self._error(409, str(error))
        except CropUnavailable as error:
            return self._error(409, f"Cannot save verified label: {error}")
        except UnknownEntry:
            return self._error(404, "Unknown gold entry")
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as error:
            return self._error(400, str(error))

    def _mutation_allowed(self) -> bool:
        supplied = self.headers.get("X-Gold-Token", "") or self.headers.get(
            "X-Review-Token", ""
        )
        if not hmac.compare_digest(supplied, self.app.token):
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = urlsplit(origin).hostname
        return host in {"127.0.0.1", "localhost", "::1"}

    def _static(self, filename: str, content_type: str):
        path = Path(__file__).parent / "gold_static" / filename
        return self._send(200, path.read_bytes(), content_type)

    def _json(self, payload, status=200):
        return self._send(
            status, (_stable_json(payload) + "\n").encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(self, status: int, message: str):
        return self._json({"error": message}, status)

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
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


def build_app(args: argparse.Namespace) -> GoldApp:
    manifest_path = args.manifest
    if manifest_path is not None and Path(manifest_path).exists():
        dataset = GoldDataset.open(Path(manifest_path))
    else:
        if args.book_root is None:
            raise SystemExit("Provide --book-root to build a manifest, or an existing --manifest")
        dataset = load_or_create_manifest(
            Path(args.book_root),
            manifest_path=(
                Path(manifest_path)
                if manifest_path is not None
                else Path(args.book_root) / "gold" / "manifest.json"
            ),
            source=args.source,
            cap=args.cap,
            sample=args.sample,
            seed=args.seed,
        )
    review_root = args.review_root or dataset.path.parent
    return GoldApp(
        dataset,
        args.review_db or review_root / "gold.sqlite3",
        source=args.source,
        cache_root=review_root / "cache",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book-root", type=Path, help="finished book work directory")
    parser.add_argument("--manifest", type=Path, help="frozen manifest (default: BOOK/gold/manifest.json)")
    parser.add_argument("--source", type=Path, help="PDF override for line crops")
    parser.add_argument("--review-db", type=Path, help="review SQLite DB (default: manifest dir gold.sqlite3)")
    parser.add_argument("--review-root", type=Path, help="cache/export directory (default: manifest directory)")
    parser.add_argument("--cap", type=int, default=DEFAULT_CAP)
    parser.add_argument("--sample", choices=("all", "mixed", "random", "suspicious"), default="mixed")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--export-tesstrain", type=Path, help="write verified PNG + .gt.txt pairs and exit")
    parser.add_argument("--host", default="127.0.0.1", help="bind address; loopback is recommended")
    parser.add_argument("--port", type=int, default=8766, help="TCP port (default: 8766)")
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("The gold website must bind to loopback")
    if args.manifest is None and args.book_root is None and args.export_tesstrain is None:
        parser.error("Provide --book-root and/or --manifest")
    app = build_app(args)
    if args.export_tesstrain is not None:
        result = app.store.export_tesstrain(args.export_tesstrain, app.cache_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        app.close()
        return 0
    server = GoldHTTPServer((args.host, args.port), app)
    print(f"Gold adjudication running at http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
