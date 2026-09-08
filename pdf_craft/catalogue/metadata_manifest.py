"""Resumable high-confidence metadata manifest for the bio10 corpus.

The manifest is a JSON file mapping each inventoried source path to an
accepted ``title``/``authors`` pair plus the unresolved candidates and the
per-field evidence that produced them.  It is the single input to cover
export (:mod:`pdf_craft.catalogue.cover_export`) and safe embedding
(:mod:`pdf_craft.catalogue.metadata_embed`).

Design rules, all machine-checkable in ``tests/catalogue/``:

* **Idempotence.** :func:`update_manifest` reuses an entry whose
  ``sha256``/``file_size``/``mtime_ns`` still match; a second run over an
  unchanged tree changes nothing.
* **Change invalidation.** Any identity change forces re-resolution of
  exactly that entry.
* **Conservative conflicts.** An accepted value needs corroboration: it
  must appear in at least two independent signals (filename, embedded,
  cover OCR), or be the only non-empty signal.  Two signals that disagree
  yield ``status="ambiguous"`` with no accepted value rather than a guess.
  Publisher/year are never inferred and stay absent unless a parser
  produced them.
* **Explicit damage accounting.** Unreadable files, unsupported
  extensions, placeholders and non-books each keep their own status, so a
  final report can distinguish completed, ambiguous, deferred and damaged
  inputs.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

from .document_metadata import build_document_metadata
from .filename_parser import parse_filename

MANIFEST_VERSION = 1

SUPPORTED_SUFFIXES = frozenset({".pdf", ".epub"})

STATUS_OK = "ok"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_EMPTY = "empty"
STATUS_NOT_A_BOOK = "not_a_book"
STATUS_UNREADABLE = "unreadable"
STATUS_UNSUPPORTED = "unsupported"
STATUS_DEFERRED = "deferred"


def sha256_of(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file without loading it into memory.  Raises ``OSError``."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def media_type_for(path: str | Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _dedupe(values: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        cleaned = " ".join(str(value).split())
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


def _signal_lists(
    source_path: str,
    media_type: str | None,
    *,
    cover_reading: Any | None = None,
    read_embedded: bool = True,
    embedded_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return per-signal title/author lists plus filename parse details.

    ``embedded_override`` exists for tests: it replaces the embedded
    read so a filename/embedded conflict can be constructed without
    crafting binary fixtures for every case.
    """
    parsed = parse_filename(source_path)
    filename_titles = list(parsed.titles)
    filename_authors = list(parsed.authors)
    embedded_titles: list[str] = []
    embedded_authors: list[str] = []
    embedded_error: str | None = None
    if embedded_override is not None:
        embedded_titles = [str(v) for v in _as_list(embedded_override.get("title"))]
        embedded_authors = [str(v) for v in _as_list(embedded_override.get("authors"))]
    elif read_embedded:
        try:
            from .epub_metadata import extract_epub_metadata
            from .pdf_metadata import extract_pdf_metadata

            is_epub = Path(source_path).suffix.lower() == ".epub" or (
                media_type or ""
            ).lower() in {"application/epub+zip", "application/epub"}
            raw = (
                extract_epub_metadata(source_path)
                if is_epub
                else extract_pdf_metadata(source_path)
            )
            embedded_titles = [str(v) for v in _as_list(raw.get("title"))]
            embedded_authors = [str(v) for v in _as_list(raw.get("authors"))]
        except Exception as exc:
            embedded_error = f"{type(exc).__name__}: {exc}"[:200]
    cover_titles = list(getattr(cover_reading, "title_candidates", ()) or [])
    cover_authors = list(getattr(cover_reading, "author_candidates", ()) or [])
    return {
        "parsed": parsed,
        "filename_titles": filename_titles,
        "filename_authors": filename_authors,
        "embedded_titles": embedded_titles,
        "embedded_authors": embedded_authors,
        "embedded_error": embedded_error,
        "cover_titles": [str(v) for v in cover_titles],
        "cover_authors": [str(v) for v in cover_authors],
    }


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _accept_title(
    filename: list[str], embedded: list[str], cover: list[str],
) -> tuple[str | None, list[str], bool]:
    """Conservative single-title choice.

    Returns ``(accepted, candidates, conflict)``.  Acceptance needs the
    top candidate corroborated by a second signal, or exactly one
    non-empty signal.  Disagreement between two or more non-empty
    signals is a conflict: no accepted value.
    """
    nonempty = [s for s in (filename, embedded, cover) if s]
    # Container precedence is applied by the caller ordering; here all
    # signals are ranked filename, embedded, cover for determinism.
    candidates = _dedupe([*filename, *embedded, *cover])
    if not candidates:
        return None, [], False
    if len(nonempty) <= 1:
        return candidates[0], candidates, False
    top = candidates[0]
    corroborated = sum(1 for s in nonempty if top in set(s)) >= 2
    if corroborated:
        return top, candidates, False
    return None, candidates, True


def _accept_authors(
    filename: list[str], embedded: list[str], cover: list[str],
) -> tuple[list[str], list[str], bool]:
    """Conservative author choice; same corroboration rule as titles.

    A single non-empty signal is accepted as-is.  When several signals
    carry authors they must share at least one name, otherwise the entry
    is ambiguous and no author is accepted.
    """
    nonempty = [s for s in (filename, embedded, cover) if s]
    candidates = _dedupe([*filename, *embedded, *cover])
    if not candidates:
        return [], [], False
    if len(nonempty) <= 1:
        return list(nonempty[0]), candidates, False
    sets = [set(s) for s in nonempty]
    shared = set.intersection(*sets)
    if shared:
        return candidates, candidates, False
    return [], candidates, True


def build_entry(
    source_path: str | Path,
    *,
    media_type: str | None = None,
    cover_reading: Any | None = None,
    read_embedded: bool = True,
    embedded_override: dict[str, Any] | None = None,
    deferred: bool = False,
) -> dict[str, Any]:
    """Resolve one file into a manifest entry dict (JSON-serializable)."""
    path = Path(source_path)
    key = str(source_path)
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        try:
            stat = path.stat()
            size, mtime = stat.st_size, stat.st_mtime_ns
        except OSError:
            size, mtime = None, None
        return {
            "source_path": key, "sha256": None, "file_size": size,
            "mtime_ns": mtime,
            "media_type": media_type or media_type_for(path),
            "status": STATUS_UNSUPPORTED, "title": None, "authors": [],
            "title_candidates": [], "author_candidates": [],
            "isbns": [], "conflict": False, "evidence": {"reason": "unsupported extension"},
        }
    try:
        stat = path.stat()
        digest = sha256_of(path)
    except OSError as exc:
        return {
            "source_path": key, "sha256": None, "file_size": None,
            "mtime_ns": None,
            "media_type": media_type or media_type_for(path),
            "status": STATUS_UNREADABLE, "title": None, "authors": [],
            "title_candidates": [], "author_candidates": [],
            "isbns": [], "conflict": False,
            "evidence": {"reason": f"unreadable: {exc}"[:200]},
        }
    resolved_media = media_type or media_type_for(path)
    parsed = parse_filename(key)
    if parsed.is_not_a_book:
        return {
            "source_path": key, "sha256": digest, "file_size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "media_type": resolved_media,
            "status": STATUS_NOT_A_BOOK, "title": None, "authors": [],
            "title_candidates": [], "author_candidates": [],
            "isbns": [], "conflict": False,
            "evidence": {"filename_source": parsed.source, "signals": []},
        }
    if deferred:
        return {
            "source_path": key, "sha256": digest, "file_size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "media_type": resolved_media,
            "status": STATUS_DEFERRED, "title": None, "authors": [],
            "title_candidates": [], "author_candidates": [],
            "isbns": [], "conflict": False,
            "evidence": {"reason": "deferred: active consumer or pilot scope"},
        }

    signals = _signal_lists(
        key, resolved_media, cover_reading=cover_reading,
        read_embedded=read_embedded, embedded_override=embedded_override,
    )
    parsed = signals["parsed"]
    filename_titles = signals["filename_titles"]
    filename_authors = signals["filename_authors"]
    filename_conf = parsed.confidence
    embedded_titles = signals["embedded_titles"]
    embedded_authors = signals["embedded_authors"]
    embedded_error = signals["embedded_error"]
    cover_titles = signals["cover_titles"]
    cover_authors = signals["cover_authors"]

    # A lone low-confidence filename (bare slug, no matched template) is
    # not evidence: accepting it would launder garbage like "doc" into
    # the catalogue.  Only a matched template (confidence >= 0.5), a
    # junk-filtered embedded read, or cover OCR can carry a lone signal.
    filename_trusted = filename_conf >= 0.5
    if (
        embedded_error is not None
        and not filename_trusted
        and not cover_titles and not cover_authors
    ):
        # The container cannot be parsed and nothing else vouches for
        # it: this is damage, not an empty-but-healthy file.
        return {
            "source_path": key, "sha256": digest, "file_size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "media_type": resolved_media,
            "status": STATUS_UNREADABLE, "title": None, "authors": [],
            "title_candidates": [], "author_candidates": [],
            "isbns": [], "conflict": False,
            "evidence": {"embedded_error": embedded_error,
                         "filename_source": parsed.source},
        }

    # EPUBs rank the OPF first, PDFs rank the filename first -- the same
    # precedence split as document_metadata.build_document_metadata.
    # An untrusted filename (bare slug, no matched template) does not
    # compete: it stays a candidate, but only trusted signals can force
    # ambiguity.  This lets cover OCR rescue slugs it was built for
    # without laundering the slug itself into an accepted value.
    trusted_titles = filename_titles if filename_trusted else []
    trusted_authors = filename_authors if filename_trusted else []
    is_epub = path.suffix.lower() == ".epub"
    if is_epub:
        contest_titles = (embedded_titles, trusted_titles, cover_titles)
        contest_authors = (embedded_authors, trusted_authors, cover_authors)
    else:
        contest_titles = (trusted_titles, embedded_titles, cover_titles)
        contest_authors = (trusted_authors, embedded_authors, cover_authors)
    # Rank candidates in container precedence order for the accepted pick.
    ranked = _dedupe([*_ordered_first(contest_titles)])
    accepted_title, title_candidates, title_conflict = _accept_title(*contest_titles)
    accepted_authors, author_candidates, author_conflict = _accept_authors(*contest_authors)
    # Untrusted filename values remain visible as low-ranked candidates.
    title_candidates = _dedupe([*title_candidates, *filename_titles])
    author_candidates = _dedupe([*author_candidates, *filename_authors])
    title_conflict = bool(title_conflict)
    author_conflict = bool(author_conflict)

    fused_payload: dict[str, Any] = {}
    try:
        fused_payload = build_document_metadata(
            key, resolved_media, read_embedded=read_embedded,
            cover_reading=cover_reading,
        )
    except Exception:
        fused_payload = {}
    isbns = [str(v) for v in _as_list((fused_payload or {}).get("isbns"))]
    extraction = (fused_payload or {}).get("_extraction", {})

    conflict = bool(title_conflict or author_conflict)
    if conflict:
        # Per-field conservatism: a conflict clears only its own field,
        # so an EPUB whose slug disagrees with its OPF title still keeps
        # the OPF authors.
        status = STATUS_AMBIGUOUS
        if title_conflict:
            accepted_title = None
        if author_conflict:
            accepted_authors = []
    elif accepted_title or accepted_authors:
        status = STATUS_OK
    else:
        status = STATUS_EMPTY

    evidence = {
        "filename_source": parsed.source,
        "filename_confidence": filename_conf,
        "filename_titles": filename_titles,
        "filename_authors": filename_authors,
        "embedded_titles": embedded_titles,
        "embedded_authors": embedded_authors,
        "cover_titles": cover_titles,
        "cover_authors": cover_authors,
        "signals": extraction.get("signals", []) if isinstance(extraction, dict) else [],
        "precedence": "embedded_first" if is_epub else "filename_first",
        "ranked_titles": ranked,
        "title_conflict": title_conflict,
        "author_conflict": author_conflict,
    }
    if embedded_error is not None:
        evidence["embedded_error"] = embedded_error
    entry: dict[str, Any] = {
        "source_path": key, "sha256": digest, "file_size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns, "media_type": resolved_media,
        "status": status, "title": accepted_title, "authors": accepted_authors,
        "title_candidates": title_candidates,
        "author_candidates": author_candidates, "isbns": isbns,
        "conflict": conflict, "evidence": evidence,
    }
    for field in ("volume", "edition", "year"):
        value = getattr(parsed, field, None)
        if value:
            entry[field] = value
    return entry


def _ordered_first(ordered: tuple[list[str], ...]) -> list[str]:
    out: list[str] = []
    for part in ordered:
        out.extend(part)
    return out


def empty_manifest() -> dict[str, Any]:
    return {"version": MANIFEST_VERSION, "entries": {}}


def load_manifest(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or "entries" not in data:
        raise ValueError(f"not a metadata manifest: {path}")
    return data


def save_manifest(manifest: dict[str, Any], path: str | Path) -> None:
    """Write atomically so an interrupted run never leaves a half file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, target)


def _normalize_path_set(paths: Any | None) -> set[str] | None:
    if paths is None:
        return None
    return {os.path.abspath(str(p).strip()) for p in paths if str(p).strip()}


def load_path_list(path: str | Path) -> set[str]:
    """Read a defer-list file: one path per line, `#` comments ignored."""
    listed: set[str] = set()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            cleaned = line.split("#", 1)[0].strip()
            if cleaned:
                listed.add(os.path.abspath(cleaned))
    return listed


def _identity_matches(entry: dict[str, Any], path: Path) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    if entry.get("file_size") != stat.st_size:
        return False
    if entry.get("mtime_ns") != stat.st_mtime_ns:
        return False
    try:
        return entry.get("sha256") == sha256_of(path)
    except OSError:
        return False


def load_cover_index(path: str | Path) -> dict[str, Any]:
    """Load cover-OCR readings for the manifest run.

    The JSON file maps each source path to
    ``{"title_candidates": [...], "author_candidates": [...]}`` -- the
    same shape as :class:`cover_ocr.CoverReading` candidates, so a cover
    pass over the corpus can feed three-signal corroboration without a
    GPU-resident model at manifest time.
    """
    from types import SimpleNamespace

    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"not a cover index: {path}")
    readings: dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            raise ValueError(f"cover index entry for {key!r} must be an object")
        readings[str(key)] = SimpleNamespace(
            title_candidates=tuple(value.get("title_candidates") or ()),
            author_candidates=tuple(value.get("author_candidates") or ()),
        )
    return readings


def update_manifest(
    manifest_path: str | Path,
    paths: list[str | Path],
    *,
    force: bool = False,
    read_embedded: bool = True,
    cover_readings: dict[str, Any] | None = None,
    deferred: Any | None = None,
) -> dict[str, int]:
    """Create or resume a manifest over *paths*.

    Entries whose file identity still matches are reused untouched
    (idempotence); changed or new paths are re-resolved (change
    invalidation).  Paths in *deferred* (e.g. under an active consumer)
    are recorded with ``status="deferred"`` instead of being resolved,
    so the final accounting distinguishes them.  The manifest is saved
    atomically at the end.
    """
    manifest: dict[str, Any]
    target = Path(manifest_path)
    if target.exists():
        manifest = load_manifest(target)
    else:
        manifest = empty_manifest()
    entries = manifest.setdefault("entries", {})
    readings = cover_readings or {}
    deferred_set = _normalize_path_set(deferred)
    report = {"examined": 0, "reused": 0, "resolved": 0,
              "ambiguous": 0, "damaged": 0, "deferred": 0}
    for raw in paths:
        key = str(raw)
        report["examined"] += 1
        existing = entries.get(key)
        want_deferred = (
            deferred_set is not None and os.path.abspath(key) in deferred_set
        )
        if deferred_set is not None and want_deferred:
            if (
                not force and existing is not None
                and existing.get("status") == STATUS_DEFERRED
                and _identity_matches(existing, Path(raw))
            ):
                report["reused"] += 1
                report["deferred"] += 1
                continue
            entries[key] = build_entry(raw, deferred=True)
            report["resolved"] += 1
            report["deferred"] += 1
            continue
        if (
            not force and existing is not None
            # A lifted deferral is a state change even when the bytes
            # are untouched: the entry must be resolved, not reused.
            and existing.get("status") != STATUS_DEFERRED
            and _identity_matches(existing, Path(raw))
        ):
            report["reused"] += 1
            continue
        entry = build_entry(
            raw, read_embedded=read_embedded,
            cover_reading=readings.get(key),
        )
        entries[key] = entry
        report["resolved"] += 1
        if entry["status"] == STATUS_AMBIGUOUS:
            report["ambiguous"] += 1
        if entry["status"] in (STATUS_UNREADABLE, STATUS_UNSUPPORTED):
            report["damaged"] += 1
    manifest["version"] = MANIFEST_VERSION
    save_manifest(manifest, target)
    return report


def iter_paths(
    root: str | Path,
    *,
    extensions: tuple[str, ...] = ("pdf", "epub"),
) -> list[str]:
    """Lexically sorted inventory of supported files (no symlinks followed)."""
    base = Path(root)
    wanted = {"." + ext.lstrip(".").lower() for ext in extensions}
    found: list[str] = []
    for entry in sorted(base.rglob("*"), key=str):
        if entry.is_symlink() or not entry.is_file():
            continue
        if entry.suffix.lower() in wanted:
            found.append(str(entry))
    return found


def summarize(manifest: dict[str, Any]) -> dict[str, int]:
    """Bucket entries into completed/ambiguous/deferred/damaged/empty."""
    counts = {"entries": 0, "completed": 0, "ambiguous": 0, "deferred": 0,
              "damaged": 0, "empty": 0, "not_a_book": 0}
    for entry in manifest.get("entries", {}).values():
        counts["entries"] += 1
        status = entry.get("status")
        if status == STATUS_OK:
            counts["completed"] += 1
        elif status == STATUS_AMBIGUOUS:
            counts["ambiguous"] += 1
        elif status == STATUS_DEFERRED:
            counts["deferred"] += 1
        elif status in (STATUS_UNREADABLE, STATUS_UNSUPPORTED):
            counts["damaged"] += 1
        elif status == STATUS_NOT_A_BOOK:
            counts["not_a_book"] += 1
        else:
            counts["empty"] += 1
    return counts
