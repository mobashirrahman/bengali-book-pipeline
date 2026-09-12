"""Loopback-only blinded adjudication website for the 5-voter study.

Integrates with the annotation server's role tokens: only an
``adjudicator`` token may read or resolve study conflicts or fetch region
crops (``annotator``/missing/invalid tokens get 403)::

    python -m pdf_craft_tool.research.study_server \\
        --db <annotation-roles.sqlite3> \\
        --votes-db pdf-craft-output/research/main-study-1000/votes.sqlite3 \\
        --voter-ids tesseract easyocr surya google azure \\
        --port 8768

Region crops are rendered from ``<study-dir>/pages/<page_id>.png`` using the
census geometry in ``<study-dir>/annotation_pages.json`` (``--study-dir``
defaults to the votes database's directory) and disk-cached under
``<study-dir>/study-crops``.

Every study payload (including error bodies, static files and crop PNGs)
passes an outbound blindness gate: voter ids and the label seed may never
cross the wire, and any violation fails closed with a generic 500. Crop PNGs
may carry only pixel/colour chunks, so no text metadata can smuggle an
identity. The per-launch adjudicator token is printed on stdout; paste it
into the page (stored in localStorage) or open ``/study#token=<token>``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .annotation import RevisionConflict
from .annotation_server import (
    MAX_IMAGE_BYTES,
    PNG_SIGNATURE,
    TOKEN_HEADER,
    AnnotationApp,
    AnnotationStore,
    padded_crop_box,
    png_size,
)
from .study_adjudication import StudyVoteStore

MAX_REQUEST_BYTES = 1_000_000
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
GENERIC_500 = "internal error"

#: PNG chunks a served crop may contain: pixels and colour handling only,
#: never text/EXIF/ICC chunks that could carry an identity string.
ALLOWED_PNG_CHUNKS = frozenset({
    b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND",
    b"gAMA", b"cHRM", b"sRGB", b"pHYs", b"sBIT", b"bKGD",
})

#: Page ids that may name a file under ``pages/`` (no separators, no dots
#: leading, bounded length) -- checked before any filesystem access.
SAFE_PAGE_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}")


def _stable_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def png_chunks(data: bytes):
    """Yield ``(chunk_type, chunk_data)`` pairs; raise on malformed PNG."""
    if data[:8] != PNG_SIGNATURE:
        raise ValueError("not a PNG image")
    offset = 8
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("truncated PNG chunk")
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("truncated PNG chunk")
        yield kind, data[offset + 8:offset + 8 + length]
        offset = end
        if kind == b"IEND":
            if offset != len(data):
                raise ValueError("data after PNG IEND")
            return
    raise ValueError("PNG has no IEND chunk")


class StudyCropSource:
    """Padded census-region crops of the study pages (hash-checked source).

    Geometry comes from ``annotation_pages.json`` (``census.entries[]`` in
    the same pixel space as the page PNGs); the index is re-read whenever
    that file changes. Renders are cached on disk keyed by the source
    image's actual sha256, region and padded box, so census amendments or a
    swapped image self-invalidate without a restart.
    """

    def __init__(self, study_dir: Path, cache_dir: Path | None = None):
        self.study_dir = Path(study_dir)
        self.pages_json = self.study_dir / "annotation_pages.json"
        self.pages_dir = self.study_dir / "pages"
        self.cache_dir = Path(cache_dir) if cache_dir is not None \
            else self.study_dir / "study-crops"
        self._lock = threading.Lock()
        self._index: dict[str, tuple[str, dict[int, dict]]] = {}
        self._index_stamp: tuple[int, int] | None = None
        # page_id -> ((st_size, st_mtime_ns), sha256) of a hashed source
        self._digests: dict[str, tuple[tuple[int, int], str]] = {}

    def _load_index(self) -> dict[str, tuple[str, dict[int, dict]]]:
        with self._lock:
            stat = self.pages_json.stat()
            stamp = (stat.st_size, stat.st_mtime_ns)
            if stamp != self._index_stamp:
                pages = json.loads(self.pages_json.read_text("utf-8"))
                index = {}
                for page in pages if isinstance(pages, list) else []:
                    if not isinstance(page, dict) or not page.get("page_id"):
                        continue
                    regions = {}
                    for entry in (page.get("census") or {}).get("entries",
                                                                 []):
                        geometry = entry.get("geometry") or {}
                        try:
                            regions[int(entry["region_index"])] = {
                                key: int(round(float(geometry[key])))
                                for key in ("x0", "y0", "x1", "y1")}
                        except (KeyError, TypeError, ValueError):
                            continue
                    index[str(page["page_id"])] = (
                        str(page.get("image_sha256") or ""), regions)
                self._index, self._index_stamp = index, stamp
            return self._index

    def _source(self, page_id: str, expected_sha: str) -> tuple[Path, str]:
        """Page PNG path and its actual sha256 (checked when frozen)."""
        path = self.pages_dir / f"{page_id}.png"
        if not path.is_file():
            raise LookupError("no image for this page")
        stat = path.stat()
        if stat.st_size > MAX_IMAGE_BYTES:
            raise ValueError("page image exceeds the size limit")
        stamp = (stat.st_size, stat.st_mtime_ns)
        known = self._digests.get(page_id)
        if known is None or known[0] != stamp:
            known = (stamp, hashlib.sha256(path.read_bytes()).hexdigest())
            self._digests[page_id] = known
        if expected_sha and known[1] != expected_sha:
            raise ValueError("page image does not match the frozen hash")
        return path, known[1]

    def crop(self, page_id: str, region_index: int) -> bytes:
        """PNG bytes of one padded census-region crop."""
        if not SAFE_PAGE_ID.fullmatch(page_id):
            raise LookupError("unknown page")
        entry = self._load_index().get(page_id)
        if entry is None:
            raise LookupError("unknown page")
        expected_sha, regions = entry
        geometry = regions.get(region_index)
        if geometry is None:
            raise LookupError("unknown region")
        path, digest = self._source(page_id, expected_sha)
        with path.open("rb") as handle:
            width, height = png_size(handle.read(24))
        box = padded_crop_box(geometry, width, height)
        if box[2] <= box[0] or box[3] <= box[1]:
            raise LookupError("census region has an empty crop box")
        cached = self.cache_dir / (f"{digest[:24]}_{region_index}_{box[0]}_"
                                   f"{box[1]}_{box[2]}_{box[3]}.png")
        if cached.is_file():
            return cached.read_bytes()
        try:
            from PIL import Image
        except ImportError as error:
            raise RuntimeError("region crops need Pillow installed") from error
        try:
            with Image.open(path) as image:
                crop = image.crop(box)
                if crop.mode not in ("1", "L", "LA", "P", "RGB", "RGBA"):
                    crop = crop.convert("RGB")
                buffer = io.BytesIO()
                # icc_profile=None: never copy source metadata into the crop.
                crop.save(buffer, format="PNG", icc_profile=None)
        except Image.DecompressionBombError as error:
            raise ValueError("page image is too large to decode") from error
        raw = buffer.getvalue()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        partial = cached.with_name(
            f".{cached.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        partial.write_bytes(raw)
        os.replace(partial, cached)
        return raw


class StudyHTTPServer(ThreadingHTTPServer):
    """HTTP server binding the study votes to annotation role tokens."""

    daemon_threads = True

    def __init__(self, address, app: AnnotationApp, votes: StudyVoteStore,
                 crops: StudyCropSource | None = None):
        host = address[0] if isinstance(address, (list, tuple)) else address
        if host not in LOOPBACK_HOSTS:
            raise ValueError(
                f"the study website must bind to loopback, got {host!r}")
        self.app = app
        self.votes = votes
        self.crops = crops
        super().__init__(address, StudyRequestHandler)

    def server_close(self):
        super().server_close()
        try:
            self.app.close()
        finally:
            self.votes.close()


class StudyRequestHandler(BaseHTTPRequestHandler):
    server: StudyHTTPServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # keep test output clean
        pass

    @property
    def app(self) -> AnnotationApp:
        return self.server.app

    @property
    def votes(self) -> StudyVoteStore:
        return self.server.votes

    def _token(self) -> str:
        return self.headers.get(TOKEN_HEADER, "") or ""

    def _adjudicator(self):
        """Identity dict when the caller holds an adjudicator token."""
        identity = self.app.identity(self._token())
        if identity is None or identity.get("role") != "adjudicator":
            return None
        return identity

    # -- routing ------------------------------------------------------
    def do_GET(self):
        route = urlsplit(self.path)
        try:
            if route.path in ("/", "/study"):
                return self._static("study_adjudicate.html",
                                    "text/html; charset=utf-8")
            if route.path == "/static/study_adjudicate.js":
                return self._static("study_adjudicate.js",
                                    "text/javascript; charset=utf-8")
            if route.path == "/api/study/pages":
                identity = self._adjudicator()
                if identity is None:
                    return self._error(403, "An adjudicator token is required")
                return self._clean(200, self._pages())
            parts = route.path.strip("/").split("/")
            if len(parts) == 4 and parts[0:2] == ["api", "study"] \
                    and parts[2] == "conflicts" and parts[3]:
                identity = self._adjudicator()
                if identity is None:
                    return self._error(403, "An adjudicator token is required")
                return self._clean(200, self._open_conflicts(parts[3]))
            if len(parts) == 5 and parts[0:3] == ["api", "study", "crop"]:
                identity = self._adjudicator()
                if identity is None:
                    return self._error(403, "An adjudicator token is required")
                return self._crop(parts[3], parts[4])
            return self._error(404, "Not found")
        except (OSError, ValueError, RuntimeError, LookupError) as error:
            return self._clean(500, {"error": str(error)},
                               generic=True)

    def do_POST(self):
        route = urlsplit(self.path)
        parts = route.path.strip("/").split("/")
        if not (len(parts) == 4 and parts[0:2] == ["api", "study"]
                and parts[2] == "adjudicate" and parts[3]):
            return self._error(404, "Not found")
        identity = self._adjudicator()
        if identity is None:
            return self._error(403, "An adjudicator token is required")
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
            for key in ("region_index", "resolved_text", "reason", "revision"):
                if key not in payload:
                    raise ValueError(f"{key} is required")
            region_index = payload["region_index"]
            if isinstance(region_index, bool) or not isinstance(region_index, int):
                raise ValueError("region_index must be an integer")
            revision = payload["revision"]
            if isinstance(revision, bool) or not isinstance(revision, int):
                raise ValueError("revision must be an integer")
            choice = payload.get("choice_label")
            if choice is not None:
                choice = str(choice).strip().upper() or None
            row = self.votes.adjudicate(
                parts[3], region_index,
                resolved_text=str(payload["resolved_text"]),
                resolver_id=identity["user_id"],
                reason=str(payload["reason"]),
                revision=revision, choice_label=choice)
            return self._clean(200, {"adjudication": {
                "page_id": row["page_id"],
                "region_index": row["region_index"],
                "revision": row["revision"],
                "choice_label": row["choice_label"],
            }})
        except RevisionConflict as error:
            return self._clean(409, {"error": str(error)})
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError,
                ValueError) as error:
            return self._clean(400, {"error": str(error)})

    # -- helpers ------------------------------------------------------
    def _pages(self) -> dict:
        pages, resolved = [], 0
        for pid in self.votes.page_ids():
            pages.append({"page_id": pid,
                          "open_conflicts": self.votes.open_conflicts(pid)})
            resolved += len(self.votes._resolved(pid))
        return {"pages": pages, "summary": {
            "pages": len(pages),
            "open_conflicts": sum(page["open_conflicts"] for page in pages),
            "resolved": resolved,
        }}

    def _open_conflicts(self, page_id: str) -> dict:
        payload = self.votes.blind_conflicts(page_id)
        done = self.votes._resolved(page_id)
        payload["items"] = [item for item in payload["items"]
                            if item["region_index"] not in done]
        return payload

    def _crop(self, page_id: str, region: str):
        if self.server.crops is None:
            return self._error(404, "Region crops are not configured")
        if not (region.isascii() and region.isdigit()) or len(region) > 9:
            return self._error(400, "region_index must be a non-negative "
                                    "integer")
        try:
            data = self.server.crops.crop(page_id, int(region))
        except LookupError:
            return self._error(404, "No such census region")
        return self._clean_png(data)

    def _forbidden(self) -> list[str]:
        forbidden = [str(v) for v in self.votes.voter_ids if v]
        seed = getattr(self.votes, "label_seed", "")
        if seed:
            forbidden.append(str(seed))
        return forbidden

    def _leaks(self, text: str) -> bool:
        lowered = text.lower()
        return any(secret and secret.lower() in lowered
                   for secret in self._forbidden())

    def _fail_closed(self):
        return self._send(500, (_stable_json({"error": GENERIC_500})
                                + "\n").encode("utf-8"),
                          "application/json; charset=utf-8")

    def _clean(self, status: int, payload: dict, generic: bool = False):
        """Send JSON only when it carries no identities; else fail closed."""
        dump = _stable_json(payload)
        if self._leaks(dump) or generic:
            return self._fail_closed()
        return self._send(status, (dump + "\n").encode("utf-8"),
                          "application/json; charset=utf-8")

    def _clean_png(self, data: bytes):
        """Send a crop only when it is a pixels-only PNG with no identities.

        Compressed IDAT bytes are not scanned (random bytes would give false
        positives); every other chunk is whitelisted and scanned.
        """
        try:
            chunks = list(png_chunks(data))
        except ValueError:
            return self._fail_closed()
        for kind, body in chunks:
            if kind not in ALLOWED_PNG_CHUNKS:
                return self._fail_closed()
            if kind != b"IDAT" and self._leaks(body.decode("latin-1")):
                return self._fail_closed()
        return self._send(200, data, "image/png")

    def _static(self, filename: str, content_type: str):
        path = Path(__file__).parent / "static" / filename
        body = path.read_bytes()
        if self._leaks(body.decode("utf-8")):
            return self._fail_closed()
        return self._send(200, body, content_type)

    def _error(self, status: int, message: str):
        return self._send(status, (_stable_json({"error": message})
                                   + "\n").encode("utf-8"),
                          "application/json; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True,
                        help="annotation SQLite file (role tokens live here)")
    parser.add_argument("--votes-db", type=Path, required=True,
                        help="StudyVoteStore SQLite file")
    parser.add_argument("--voter-ids", nargs="+", required=True,
                        help="exactly the 5 voter ids of the votes DB")
    parser.add_argument("--adjudicator-id", default="human-adjudicator")
    parser.add_argument("--study-dir", type=Path, default=None,
                        help="dir holding pages/*.png and "
                             "annotation_pages.json (default: the votes "
                             "DB's directory)")
    parser.add_argument("--crop-cache", type=Path, default=None,
                        help="crop render cache (default: "
                             "<study-dir>/study-crops)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; loopback only")
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args(argv)
    if args.host not in LOOPBACK_HOSTS:
        parser.error("The study website must bind to loopback")
    voter_ids = tuple(args.voter_ids or [])
    if len(voter_ids) != 5 or len(set(voter_ids)) != 5 \
            or not all(voter_ids):
        parser.error("--voter-ids must hold exactly 5 distinct ids")
    study_dir = Path(args.study_dir or Path(args.votes_db).parent)
    crops = None
    if (study_dir / "annotation_pages.json").is_file():
        crops = StudyCropSource(study_dir, args.crop_cache)
    else:
        print(f"warning: no {study_dir / 'annotation_pages.json'}; "
              "region crops disabled", flush=True)
    store = AnnotationStore(Path(args.db), pages=[])
    app = AnnotationApp(store)
    votes = StudyVoteStore(Path(args.votes_db), voter_ids=voter_ids)
    if tuple(votes.voter_ids) != voter_ids:
        parser.error("voter ids do not match the votes database")
    server = StudyHTTPServer((args.host, args.port), app, votes, crops)
    token = app.register_adjudicator(args.adjudicator_id)
    print(f"Study adjudication at http://{args.host}:{args.port}/study",
          flush=True)
    print(f"Adjudicator token: {token}", flush=True)
    print(f"One-click open: http://{args.host}:{args.port}/study#token={token}",
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
