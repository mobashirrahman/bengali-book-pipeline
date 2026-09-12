"""Loopback-only website for blind independent annotation (S2).

Serves one annotator at a time per token: an annotator only ever receives
their own blind payload (page image hash, own census structure, their own
draft texts) and posts their own transcription, one region or a whole page
at a time. Missed text can be proposed as a new region; proposals join the
census only after reviewer approval. Adjudicator tokens unlock the
conflict, adjudication and proposal-review routes. Nothing here trains or
calls a model.

Usage::

    python -m pdf_craft_tool.research.annotation_server \
        --db /tmp/annot.sqlite3 --pages pages.json \
        --annotators ann1,ann2 --adjudicators judge1

Easy phone login (name + short PIN instead of pasted bearer tokens)::

    printf 'ann1:4821\nann2:9374\njudge1:2055\n' > /tmp/pins.txt
    chmod 600 /tmp/pins.txt
    python -m ... --pin-file /tmp/pins.txt

Pages also resolve as short ``pilot-NN`` aliases; ``GET /api/my-pages``
lists an annotator's own pages with per-page status.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .annotation import AnnotationStore, RevisionConflict

MAX_REQUEST_BYTES = 2_000_000
MAX_IMAGE_BYTES = 25_000_000
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TOKEN_HEADER = "X-Annotation-Token"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: Login brute-force guard: failures before a temporary lockout, and the
#: lockout duration. In-memory only; a restart clears both.
MAX_LOGIN_FAILURES = 10
LOGIN_LOCKOUT_SECONDS = 300

#: Short human-typable page names are ``pilot-01`` … (zero-padded).
ALIAS_PREFIX = "pilot"

#: Region crops carry this padding fraction per side, so neighbouring line
#: edges stay faintly visible for hyphenation and ambiguity cues.
CROP_PADDING_FRACTION = 0.08


def padded_crop_box(geometry: dict, width: int,
                    height: int) -> tuple[int, int, int, int]:
    """Padded ``(x0, y0, x1, y1)`` crop window, clamped to the image."""
    pad_x = int((geometry["x1"] - geometry["x0"]) * CROP_PADDING_FRACTION)
    pad_y = int((geometry["y1"] - geometry["y0"]) * CROP_PADDING_FRACTION)
    return (max(0, geometry["x0"] - pad_x),
            max(0, geometry["y0"] - pad_y),
            min(width, geometry["x1"] + pad_x),
            min(height, geometry["y1"] + pad_y))


class LoginLocked(RuntimeError):
    """Too many failed logins; the account is temporarily locked."""


def png_size(data: bytes) -> tuple[int, int]:
    """Return ``(width, height)`` from PNG bytes (IHDR only, no decoding)."""
    if len(data) < 24 or data[:8] != PNG_SIGNATURE:
        raise ValueError("not a PNG image")
    if data[12:16] != b"IHDR":
        raise ValueError("PNG header is not IHDR-first")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        raise ValueError("PNG dimensions must be positive")
    return width, height


class AnnotationApp:
    def __init__(self, store: AnnotationStore, *,
                 static_dir: Path | None = None,
                 image_dir: Path | None = None,
                 crop_cache: Path | None = None):
        self.store = store
        self.static_dir = Path(
            static_dir if static_dir is not None
            else Path(__file__).parent / "static"
        )
        self.image_dir = Path(image_dir) if image_dir is not None else None
        if crop_cache is None:
            crop_cache = Path(store.path).parent / "crops"
        self.crop_cache = Path(crop_cache)
        self.crop_cache.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # token -> {"user_id": str, "role": "annotator" | "adjudicator"}
        self._tokens: dict[str, dict] = {}
        # user_id -> {"count": int, "locked_until": float}
        self._login_failures: dict[str, dict] = {}
        # user_id -> sha256 hex of the PIN (set via set_pins, never logged)
        self._pin_hashes: dict[str, str] = {}
        # alias ("pilot-01") -> page_id, and back. Rebuilt from the store.
        self._alias_to_page: dict[str, str] = {}
        self._page_to_alias: dict[str, str] = {}
        self.rebuild_aliases()

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

    def page_image_size(self, page_id: str) -> tuple[int, int]:
        """Pixel ``(width, height)`` of a page's frozen scan for overlays."""
        data, _ = self.page_image(page_id)
        return png_size(data)

    def region_crop(self, page_id: str, region_index: int) -> bytes:
        """Padded PNG crop of one census region (hash-checked source).

        Crops are deterministic for a frozen census and cached on disk,
        keyed by image hash plus padded box coordinates, so census
        amendments self-invalidate.
        """
        try:
            from PIL import Image
        except ImportError as error:
            raise RuntimeError(
                "region crops need Pillow installed") from error
        geometry = self.store.region_geometry(page_id, region_index)
        data, _ = self.page_image(page_id)
        expected = self.store.page_image_sha256(page_id)
        image = Image.open(io.BytesIO(data))
        width, height = image.size
        box = padded_crop_box(geometry, width, height)
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError("census region has an empty crop box")
        key = (f"{expected[:16]}_{region_index}_{box[0]}_{box[1]}_"
               f"{box[2]}_{box[3]}.png")
        cached = self.crop_cache / key
        if cached.is_file():
            return cached.read_bytes()
        crop = image.crop(box)
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        raw = buffer.getvalue()
        cached.write_bytes(raw)
        return raw

    # -- easy login (name + PIN) --------------------------------------
    def set_pins(self, pins: dict[str, str]) -> None:
        """Install ``{user_id: pin}`` credentials (PINs are hashed)."""
        with self._lock:
            self._pin_hashes = {
                user_id: hashlib.sha256(pin.encode("utf-8")).hexdigest()
                for user_id, pin in pins.items()
            }

    def login_locked_out(self, user_id: str) -> bool:
        with self._lock:
            record = self._login_failures.get(user_id)
            return bool(record and record["locked_until"] > time.time())

    def login(self, user_id: str, pin: str) -> str:
        """Check a name+PIN pair and mint a token (raises on failure)."""
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("user_id must be a non-empty string")
        if not isinstance(pin, str) or not pin:
            raise ValueError("pin must be a non-empty string")
        with self._lock:
            if self.login_locked_out(user_id):
                raise LoginLocked(
                    f"user {user_id!r} is temporarily locked out; "
                    "try again later"
                )
            if not self._pin_hashes:
                raise ValueError("logins are not configured on this server")
            expected = self._pin_hashes.get(user_id)
            known_role = None
            for token, identity in self._tokens.items():
                if identity["user_id"] == user_id:
                    known_role = identity["role"]
                    break
            digest = hashlib.sha256(pin.encode("utf-8")).hexdigest()
            if expected is None or known_role is None or not hmac.compare_digest(
                digest, expected
            ):
                record = self._login_failures.setdefault(
                    user_id, {"count": 0, "locked_until": 0.0})
                record["count"] += 1
                if record["count"] >= MAX_LOGIN_FAILURES:
                    record["locked_until"] = time.time() + LOGIN_LOCKOUT_SECONDS
                raise ValueError("unknown user or wrong PIN")
            self._login_failures.pop(user_id, None)
            return self._mint(user_id, known_role)

    # -- short page aliases -------------------------------------------
    def rebuild_aliases(self) -> dict[str, str]:
        """Map deterministic ``pilot-NN`` aliases onto stored page ids."""
        with self._lock:
            ids = self.store.page_ids()
            width = max(2, len(str(len(ids))))
            self._alias_to_page = {
                f"{ALIAS_PREFIX}-{position:0{width}d}": page_id
                for position, page_id in enumerate(ids, start=1)
            }
            self._page_to_alias = {page: alias for alias, page
                                   in self._alias_to_page.items()}
            return dict(self._alias_to_page)

    def resolve_page(self, ref: str) -> str:
        """Accept a short alias or a full page id; return the page id."""
        with self._lock:
            if ref in self._alias_to_page:
                return self._alias_to_page[ref]
        if isinstance(ref, str) and len(ref) == 64:
            return ref
        raise ValueError(f"unknown page {ref!r}")

    def alias_for(self, page_id: str) -> str | None:
        with self._lock:
            return self._page_to_alias.get(page_id)

    def close(self) -> None:
        self.store.close()

    def register_annotator(self, annotator_id: str) -> str:
        return self._mint(annotator_id, "annotator")

    def register_adjudicator(self, adjudicator_id: str) -> str:
        return self._mint(adjudicator_id, "adjudicator")

    def register_coverage(self, reviewer_id: str) -> str:
        return self._mint(reviewer_id, "coverage")

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
        # Set before binding so a bind failure cannot leave server_close
        # without an app to close.
        self.app = app
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

    def _role_in(self, *roles: str) -> dict | None:
        """Identity when the caller holds one of ``roles``, else None."""
        identity = self.app.identity(self._token())
        if identity is None or identity["role"] not in roles:
            return None
        return identity

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
            if route.path == "/api/my-pages":
                identity = self.app.identity(self._token())
                if identity is None or identity["role"] != "annotator":
                    return self._error(403, "A per-annotator token is required")
                try:
                    overview = self.app.store.assignment_overview(
                        identity["user_id"])
                except ValueError as error:
                    return self._error(400, str(error))
                items = []
                for entry in overview:
                    alias = self.app.alias_for(entry["page_id"])
                    if alias is None:
                        continue
                    items.append({
                        "alias": alias,
                        "page_id": entry["page_id"],
                        "status": entry["status"],
                        "drafted_regions": entry["drafted_regions"],
                        "total_regions": entry["total_regions"],
                    })
                return self._json({"pages": items})
            parts = route.path.strip("/").split("/")
            if (len(parts) == 6 and parts[0:2] == ["api", "page"]
                    and parts[2] and parts[3] == "region" and parts[4]
                    and parts[5] == "image"):
                identity = self._role_in("annotator", "adjudicator",
                                         "coverage")
                if identity is None:
                    return self._error(403, "A valid role token is required")
                try:
                    page_id = self.app.resolve_page(parts[2])
                    region_index = int(parts[4])
                    data = self.app.region_crop(page_id, region_index)
                except (TypeError, ValueError) as error:
                    return self._error(400, str(error))
                except RuntimeError as error:
                    return self._error(500, str(error))
                return self._send(200, data, "image/png")
            if (len(parts) == 4 and parts[0:2] == ["api", "page"]
                    and parts[2] and parts[3] == "image"):
                identity = self.app.identity(self._token())
                if identity is None:
                    return self._error(403, "A valid token is required")
                if identity["role"] == "annotator":
                    return self._error(
                        403, "Transcribers view the full page only through "
                             "the peek endpoint, so peeks stay logged")
                if identity["role"] not in ("adjudicator", "coverage"):
                    return self._error(403, "A valid token is required")
                try:
                    page_id = self.app.resolve_page(parts[2])
                    data, content_type = self.app.page_image(page_id)
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
                    page_id = self.app.resolve_page(parts[2])
                    payload = self.app.store.assign(
                        page_id, identity["user_id"],
                        image_sha256=image_sha256)
                    payload["alias"] = self.app.alias_for(page_id)
                    try:
                        width, height = self.app.page_image_size(page_id)
                    except (FileNotFoundError, ValueError):
                        width, height = None, None
                    payload["image_size"] = (
                        {"width": width, "height": height}
                        if width is not None else None
                    )
                    if width is not None:
                        for slot in payload["line_slots"]:
                            x0, y0, x1, y1 = padded_crop_box(
                                slot["geometry"], width, height)
                            slot["crop_box"] = {
                                "x0": x0, "y0": y0, "x1": x1, "y1": y1}
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
                    page_id = self.app.resolve_page(parts[2])
                    conflicts = self.app.store.list_conflicts(page_id)
                    stats = (self.app.store.disagreement_stats(page_id)
                             if self.app.store.peer_ready(page_id) else None)
                except ValueError as error:
                    return self._error(409, str(error))
                return self._json({"conflicts": conflicts, "stats": stats})
            if len(parts) == 2 and parts[0:2] == ["api", "proposals"]:
                if self._role_in("adjudicator", "coverage") is None:
                    return self._error(
                        403, "A reviewer token is required")
                query = parse_qs(route.query)
                wanted = query.get("status", [None])[0]
                try:
                    proposals = self.app.store.list_proposals(status=wanted)
                except ValueError as error:
                    return self._error(400, str(error))
                return self._json({"proposals": proposals})
            if len(parts) == 3 and parts[0:3] == ["api", "coverage", "pages"]:
                if self._role_in("coverage") is None:
                    return self._error(
                        403, "A coverage-reviewer token is required")
                items = []
                for entry in self.app.store.coverage_queue():
                    alias = self.app.alias_for(entry["page_id"])
                    if alias is None:
                        continue
                    items.append({"alias": alias, **entry})
                return self._json({"pages": items})
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
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "page" \
                and parts[3] == "propose-region" and parts[2]:
            return self._propose_region(parts[2])
        if len(parts) == 3 and parts[0:2] == ["api", "adjudicate"] and parts[2]:
            return self._adjudicate(parts[2])
        if (len(parts) == 4 and parts[0:2] == ["api", "conflicts"]
                and parts[3] == "detect" and parts[2]):
            return self._detect_conflicts(parts[2])
        if len(parts) == 4 and parts[0:2] == ["api", "proposals"] \
                and parts[3] == "review" and parts[2]:
            return self._review_proposal(parts[2])
        if len(parts) == 6 and parts[0:2] == ["api", "page"] \
                and parts[2] and parts[3] == "region" and parts[4] \
                and parts[5] == "peek":
            return self._peek(parts[2], parts[4])
        if len(parts) == 5 and parts[0:3] == ["api", "coverage", "pages"] \
                and parts[3] and parts[4] == "signoff":
            return self._signoff(parts[3])
        if len(parts) == 2 and parts[0:2] == ["api", "login"]:
            return self._login()
        return self._error(404, "Not found")

    def _login(self):
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise TypeError("JSON body must be an object")
            if "user_id" not in payload or "pin" not in payload:
                raise ValueError("user_id and pin are required")
            user_id = payload["user_id"]
            if self.app.login_locked_out(user_id):
                return self._error(
                    429, "too many failed logins; try again later")
            try:
                token = self.app.login(user_id, payload["pin"])
            except LoginLocked:
                return self._error(
                    429, "too many failed logins; try again later")
            except ValueError:
                return self._error(401, "unknown user or wrong PIN")
            identity = self.app.identity(token)
            return self._json(
                {"token": token, "user_id": user_id,
                 "role": identity["role"]}, 200)
        except (TypeError, ValueError) as error:
            return self._error(400, str(error))

    def _detect_conflicts(self, page_ref: str):
        identity = self.app.identity(self._token())
        if identity is None or identity["role"] != "adjudicator":
            return self._error(403, "An adjudicator token is required")
        if not self._mutation_allowed():
            return self._error(403, "Missing or invalid token/origin")
        try:
            page_id = self.app.resolve_page(page_ref)
            conflicts = self.app.store.detect_conflicts(page_id)
            stats = self.app.store.disagreement_stats(page_id)
        except RevisionConflict as error:
            return self._error(409, str(error))
        except (TypeError, ValueError) as error:
            return self._error(409, str(error))
        return self._json({"conflicts": conflicts, "stats": stats}, 200)

    def _submit(self, page_ref: str):
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
            page_id = self.app.resolve_page(page_ref)
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

    def _propose_region(self, page_ref: str):
        identity = self.app.identity(self._token())
        if identity is None or identity["role"] != "annotator":
            return self._error(403, "A per-annotator token is required")
        if not self._mutation_allowed():
            return self._error(403, "Missing or invalid token/origin")
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise TypeError("JSON body must be an object")
            if "geometry" not in payload:
                raise ValueError("geometry is required")
            page_id = self.app.resolve_page(page_ref)
            proposal = self.app.store.propose_region(
                page_id,
                identity["user_id"],
                geometry=payload["geometry"],
                note=payload.get("note", ""),
            )
            return self._json({"proposal": proposal}, 200)
        except RevisionConflict as error:
            return self._error(409, str(error))
        except (TypeError, ValueError) as error:
            return self._error(400, str(error))

    def _peek(self, page_ref: str, region_ref: str):
        identity = self.app.identity(self._token())
        if identity is None or identity["role"] != "annotator":
            return self._error(403, "A per-annotator token is required")
        if not self._mutation_allowed():
            return self._error(403, "Missing or invalid token/origin")
        try:
            page_id = self.app.resolve_page(page_ref)
            region_index = int(region_ref)
            logged = self.app.store.log_peek(
                page_id, identity["user_id"], region_index)
            data, content_type = self.app.page_image(page_id)
        except (TypeError, ValueError) as error:
            return self._error(400, str(error))
        except FileNotFoundError as error:
            return self._error(404, str(error))
        return self._send(200, data, content_type,
                          extra={"X-Peek-Id": str(logged["id"])})

    def _signoff(self, page_ref: str):
        identity = self._role_in("coverage")
        if identity is None:
            return self._error(
                403, "A coverage-reviewer token is required")
        if not self._mutation_allowed():
            return self._error(403, "Missing or invalid token/origin")
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise TypeError("JSON body must be an object")
            if "approved" not in payload:
                raise ValueError("approved is required")
            page_id = self.app.resolve_page(page_ref)
            result = self.app.store.set_census_signoff(
                page_id, identity["user_id"],
                approved=payload["approved"])
            return self._json({"signoff": result}, 200)
        except (TypeError, ValueError) as error:
            return self._error(400, str(error))

    def _review_proposal(self, proposal_id: str):
        identity = self._role_in("adjudicator", "coverage")
        if identity is None:
            return self._error(403, "A reviewer token is required")
        if not self._mutation_allowed():
            return self._error(403, "Missing or invalid token/origin")
        try:
            try:
                numeric_id = int(proposal_id)
            except (TypeError, ValueError):
                raise ValueError(
                    f"proposal id must be an integer, got {proposal_id!r}"
                ) from None
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise TypeError("JSON body must be an object")
            if "decision" not in payload:
                raise ValueError("decision is required")
            result = self.app.store.review_proposal(
                numeric_id,
                identity["user_id"],
                decision=payload["decision"],
                reason=payload.get("reason", ""),
            )
            return self._json(
                {"proposal": result,
                 "progress": self.app.store.progress()}, 200)
        except RevisionConflict as error:
            return self._error(409, str(error))
        except (TypeError, ValueError) as error:
            return self._error(400, str(error))

    def _adjudicate(self, page_ref: str):
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
            page_id = self.app.resolve_page(page_ref)
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


def read_pin_file(path: Path) -> dict[str, str]:
    """Read ``user:pin`` credentials (one per line, ``#`` comments)."""
    pins: dict[str, str] = {}
    for lineno, raw in enumerate(
            Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.count(":") != 1:
            raise SystemExit(
                f"{path}:{lineno}: expected exactly one 'user:pin' pair")
        user_id, pin = (part.strip() for part in line.split(":", 1))
        if not user_id or not pin or any(
                char.isspace() for char in user_id + pin):
            raise SystemExit(
                f"{path}:{lineno}: user and pin must be non-blank "
                "without whitespace")
        if user_id in pins:
            raise SystemExit(f"{path}:{lineno}: duplicate user {user_id!r}")
        pins[user_id] = pin
    if not pins:
        raise SystemExit(f"{path}: no credentials found")
    return pins


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
    crop_cache = getattr(args, "crop_cache", None)
    app = AnnotationApp(store, image_dir=image_dir, crop_cache=crop_cache)
    for name in args.annotators or []:
        print(f"annotator {name}: {app.register_annotator(name)}", flush=True)
    for name in args.adjudicators or []:
        print(f"adjudicator {name}: {app.register_adjudicator(name)}",
              flush=True)
    for name in getattr(args, "coverage", []) or []:
        print(f"coverage {name}: {app.register_coverage(name)}", flush=True)
    pin_file = getattr(args, "pin_file", None)
    if pin_file is not None:
        pins = read_pin_file(pin_file)
        users = (set(args.annotators or []) | set(args.adjudicators or [])
                 | set(getattr(args, "coverage", []) or []))
        missing = sorted(users - set(pins))
        if missing:
            raise SystemExit(
                f"pin file {pin_file} has no PIN for: "
                + ", ".join(missing))
        app.set_pins(pins)
        print(f"easy login ready for {len(users)} users "
              f"({', '.join(sorted(users))}); aliases "
              f"{ALIAS_PREFIX}-01..{ALIAS_PREFIX}-{len(app._alias_to_page):02d}",
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
    parser.add_argument("--coverage", default="",
                        help="comma-separated coverage-reviewer ids to mint "
                             "tokens for")
    parser.add_argument("--crop-cache", type=Path, default=None,
                        help="directory for cached region crops "
                             "(default: <db-dir>/crops)")
    parser.add_argument("--pin-file", type=Path, default=None,
                        help="easy-login credentials: one 'user:pin' per "
                             "line; every registered user needs a PIN")
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
    args.coverage = [item for item in
                     (part.strip() for part in args.coverage.split(","))
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
