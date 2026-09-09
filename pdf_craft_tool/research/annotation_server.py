"""Loopback-only website for blind independent annotation (S2).

Serves one annotator at a time per token: an annotator only ever receives
their own blind payload (page image hash, own census structure, empty line
slots) and posts their own transcription. Adjudicator tokens unlock the
conflict and adjudication routes. Nothing here trains or calls a model.

Usage::

    python -m pdf_craft_tool.research.annotation_server \\
        --db /tmp/annot.sqlite3 --pages pages.json \\
        --annotators ann1,ann2 --adjudicators judge1
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .annotation import AnnotationStore, RevisionConflict

MAX_REQUEST_BYTES = 2_000_000
MAX_IMAGE_BYTES = 25_000_000
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TOKEN_HEADER = "X-Annotation-Token"


class AnnotationApp:
    def __init__(self, store: AnnotationStore, *,
                 static_dir: Path | None = None,
                 image_dir: Path | None = None):
        self.store = store
        self.static_dir = Path(
            static_dir if static_dir is not None
            else Path(__file__).parent / "static"
        )
        self.image_dir = Path(image_dir) if image_dir is not None else None
        self._lock = threading.RLock()
        # token -> {"user_id": str, "role": "annotator" | "adjudicator"}
        self._tokens: dict[str, dict] = {}

    def page_image(self, page_id: str) -> tuple[bytes, str]:
        """Return ``(png_bytes, content_type)`` for a page's frozen scan.

        The file ``<image_dir>/<page_id>.png`` is served only when its
        sha256 matches the hash frozen in the store, so a swapped image is
        rejected. Only the image bytes cross the wire -- never OCR/peer text.
        """
        if self.image_dir is None:
            raise FileNotFoundError("no image directory configured")
        expected = self.store.page_image_sha256(page_id)  # raises on unknown id
        path = self.image_dir / f"{page_id}.png"
        if not path.is_file():
            raise FileNotFoundError(f"no image file for page {page_id}")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("page image exceeds the size limit")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("page image does not match the frozen hash")
        return data, "image/png"

    def close(self) -> None:
        self.store.close()

    def register_annotator(self, annotator_id: str) -> str:
        return self._mint(annotator_id, "annotator")

    def register_adjudicator(self, adjudicator_id: str) -> str:
        return self._mint(adjudicator_id, "adjudicator")

    def _mint(self, user_id: str, role: str) -> str:
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("user id must be a non-empty string")
        with self._lock:
            for token, identity in self._tokens.items():
                if identity == {"user_id": user_id, "role": role}:
                    return token
            token = secrets.token_urlsafe(24)
            self._tokens[token] = {"user_id": user_id, "role": role}
            return token

    def identity(self, token: str) -> dict | None:
        with self._lock:
            items = list(self._tokens.items())
        for known, identity in items:
            if hmac.compare_digest(token, known):
                return identity
        return None

    def session(self, token: str | None = None) -> dict:
        payload: dict = {"progress": self.store.progress()}
        if token:
            found = self.identity(token)
            if found is not None:
                payload["user_id"] = found["user_id"]
                payload["role"] = found["role"]
        return payload


class AnnotationHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, app: AnnotationApp):
        super().__init__(address, AnnotationRequestHandler)
        self.app = app

    def server_close(self):
        super().server_close()
        self.app.close()


class AnnotationRequestHandler(BaseHTTPRequestHandler):
    server: AnnotationHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        return

    @property
    def app(self) -> AnnotationApp:
        return self.server.app

    def _token(self) -> str:
        header = self.headers.get(TOKEN_HEADER, "") or ""
        if header:
            return header
        query = parse_qs(urlsplit(self.path).query)
        return query.get("token", [""])[0]

    # -- GET ----------------------------------------------------------
    def do_GET(self):
        route = urlsplit(self.path)
        try:
            if route.path == "/":
                return self._static("index.html", "text/html; charset=utf-8")
            if route.path == "/static/app.js":
                return self._static("app.js", "text/javascript; charset=utf-8")
            if route.path == "/static/styles.css":
                return self._static("styles.css", "text/css; charset=utf-8")
            if route.path == "/api/session":
                return self._json(self.app.session(self._token() or None))
            parts = route.path.strip("/").split("/")
            if (len(parts) == 4 and parts[0:2] == ["api", "page"]
                    and parts[2] and parts[3] == "image"):
                identity = self.app.identity(self._token())
                if identity is None or identity["role"] not in (
                        "annotator", "adjudicator"):
                    return self._error(403, "A valid token is required")
                try:
                    data, content_type = self.app.page_image(parts[2])
                except FileNotFoundError as error:
                    return self._error(404, str(error))
                except ValueError as error:
                    return self._error(409, str(error))
                return self._send(200, data, content_type)
            if len(parts) == 3 and parts[0:2] == ["api", "page"] and parts[2]:
                identity = self.app.identity(self._token())
                if identity is None or identity["role"] != "annotator":
                    return self._error(403, "A per-annotator token is required")
                query = parse_qs(route.query)
                image_sha256 = query.get("image_sha256", [None])[0]
                try:
                    payload = self.app.store.assign(
                        parts[2], identity["user_id"],
                        image_sha256=image_sha256)
                except ValueError as error:
                    return self._error(404, str(error))
                return self._json(payload)
            if (len(parts) == 3 and parts[0:2] == ["api", "conflicts"]
                    and parts[2]):
                # Read-only: never mutates. Detection is POST .../detect.
                identity = self.app.identity(self._token())
                if identity is None or identity["role"] != "adjudicator":
                    return self._error(403, "An adjudicator token is required")
                try:
                    conflicts = self.app.store.list_conflicts(parts[2])
                    stats = (self.app.store.disagreement_stats(parts[2])
                             if self.app.store.peer_ready(parts[2]) else None)
                except ValueError as error:
                    return self._error(409, str(error))
                return self._json({"conflicts": conflicts, "stats": stats})
            return self._error(404, "Not found")
        except (OSError, ValueError, RuntimeError) as error:
            return self._error(500, str(error))

    # -- POST ---------------------------------------------------------
    def do_POST(self):
        route = urlsplit(self.path)
        parts = route.path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "page" \
                and parts[3] == "submit" and parts[2]:
            return self._submit(parts[2])
        if len(parts) == 3 and parts[0:2] == ["api", "adjudicate"] and parts[2]:
            return self._adjudicate(parts[2])
        if (len(parts) == 4 and parts[0:2] == ["api", "conflicts"]
                and parts[3] == "detect" and parts[2]):
            return self._detect_conflicts(parts[2])
        return self._error(404, "Not found")

    def _detect_conflicts(self, page_id: str):
        identity = self.app.identity(self._token())
        if identity is None or identity["role"] != "adjudicator":
            return self._error(403, "An adjudicator token is required")
        if not self._mutation_allowed():
            return self._error(403, "Missing or invalid token/origin")
        try:
            conflicts = self.app.store.detect_conflicts(page_id)
            stats = self.app.store.disagreement_stats(page_id)
        except RevisionConflict as error:
            return self._error(409, str(error))
        except (TypeError, ValueError) as error:
            return self._error(409, str(error))
        return self._json({"conflicts": conflicts, "stats": stats}, 200)

    def _submit(self, page_id: str):
        identity = self.app.identity(self._token())
        if identity is None or identity["role"] != "annotator":
            return self._error(403, "A per-annotator token is required")
        if not self._mutation_allowed():
            return self._error(403, "Missing or invalid token/origin")
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise TypeError("JSON body must be an object")
            if "revision" not in payload or "lines" not in payload:
                raise ValueError("revision and lines are required")
            result = self.app.store.submit(
                page_id,
                identity["user_id"],
                revision=payload["revision"],
                lines=payload["lines"],
                elapsed_ms=int(payload.get("elapsed_ms", 0)),
            )
            return self._json(
                {"assignment": result,
                 "progress": self.app.store.progress()}, 200)
        except RevisionConflict as error:
            return self._error(409, str(error))
        except (TypeError, ValueError) as error:
            return self._error(400, str(error))

    def _adjudicate(self, page_id: str):
        identity = self.app.identity(self._token())
        if identity is None or identity["role"] != "adjudicator":
            return self._error(403, "An adjudicator token is required")
        if not self._mutation_allowed():
            return self._error(403, "Missing or invalid token/origin")
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise TypeError("JSON body must be an object")
            for key in ("region_index", "resolved_text", "reason", "revision"):
                if key not in payload:
                    raise ValueError(f"{key} is required")
            row = self.app.store.adjudicate(
                page_id,
                int(payload["region_index"]),
                resolved_text=payload["resolved_text"],
                resolver_id=identity["user_id"],
                reason=payload["reason"],
                revision=payload["revision"],
            )
            return self._json(
                {"adjudication": row,
                 "progress": self.app.store.progress()}, 200)
        except RevisionConflict as error:
            return self._error(409, str(error))
        except (TypeError, ValueError) as error:
            return self._error(400, str(error))

    # -- helpers --------------------------------------------------------
    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request is too large")
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON: {error}") from error

    def _mutation_allowed(self) -> bool:
        if self.app.identity(self._token()) is None:
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = urlsplit(origin).hostname
        return host in LOOPBACK_HOSTS

    def _static(self, filename: str, content_type: str):
        path = self.app.static_dir / filename
        if not path.is_file():
            return self._error(404, "Not found")
        return self._send(200, path.read_bytes(), content_type)

    def _json(self, payload, status=200):
        return self._send(
            status, (_stable_json(payload) + "\n").encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(self, status: int, message: str):
        return self._json({"error": message}, status)

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def _stable_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def build_app(args: argparse.Namespace) -> AnnotationApp:
    pages: list = []
    if args.pages is not None:
        pages = json.loads(Path(args.pages).read_text(encoding="utf-8"))
        if not isinstance(pages, list):
            raise SystemExit("--pages must be a JSON list of page dicts")
    store = AnnotationStore(args.db, pages=pages)
    image_dir = getattr(args, "images", None)
    if image_dir is not None and not Path(image_dir).is_dir():
        raise SystemExit(f"--images {image_dir} is not a directory")
    app = AnnotationApp(store, image_dir=image_dir)
    for name in args.annotators or []:
        print(f"annotator {name}: {app.register_annotator(name)}", flush=True)
    for name in args.adjudicators or []:
        print(f"adjudicator {name}: {app.register_adjudicator(name)}",
              flush=True)
    return app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True,
                        help="private annotation SQLite file")
    parser.add_argument("--pages", type=Path,
                        help="JSON list of frozen page dicts (seed on first run)")
    parser.add_argument("--images", type=Path, default=None,
                        help="directory of <page_id>.png scans to serve "
                             "(hash-checked against the frozen page)")
    parser.add_argument("--annotators", default="",
                        help="comma-separated annotator ids to mint tokens for")
    parser.add_argument("--adjudicators", default="",
                        help="comma-separated adjudicator ids to mint tokens for")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; loopback only")
    parser.add_argument("--port", type=int, default=8767,
                        help="TCP port (default: 8767)")
    args = parser.parse_args(argv)
    if args.host not in LOOPBACK_HOSTS:
        parser.error("The annotation website must bind to loopback")
    args.annotators = [item for item in
                       (part.strip() for part in args.annotators.split(","))
                       if item]
    args.adjudicators = [item for item in
                         (part.strip() for part in args.adjudicators.split(","))
                         if item]
    app = build_app(args)
    server = AnnotationHTTPServer((args.host, args.port), app)
    print(f"Annotation website running at http://{args.host}:{args.port}/",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
