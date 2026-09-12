"""Safe staged embedding of accepted manifest metadata.

Embedding changes file hashes, so it must come *after* deduplication and
may only touch live sources through the explicit :func:`apply_plan`
step.  The workflow is:

1. :func:`prepare_plan` copies each accepted source into a staging
   directory and writes the metadata into the *copy* -- PDF ``/Info``
   ``/Title``/``/Author`` and EPUB OPF ``dc:title``/``dc:creator``.
   Sources are only ever opened for reading here.
2. :func:`verify_plan` re-reads every staged copy through the same
   extractors the catalogue uses and confirms the accepted values
   round-trip, independently of the write path.
3. :func:`apply_plan` copies staged files back over their sources
   atomically, but only when the live source still hashes to the
   ``source_sha256`` recorded at prepare time (changed-source
   rejection).  Already-applied sources are skipped, so apply is
   resumable and doubles as recovery.

Only ``title`` and ``authors`` are ever embedded.  Publisher/year are
never inferred and are left untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

from .epub_metadata import extract_epub_metadata
from .metadata_manifest import load_manifest, sha256_of
from .pdf_metadata import extract_pdf_metadata

EMBED_TOOL_VERSION = "1.0"

_DC_NS = "http://purl.org/dc/elements/1.1/"


# --- Low-level writers -----------------------------------------------------


def embed_pdf_metadata(
    source: str | Path, dest: str | Path,
    title: str | None, authors: list[str],
) -> None:
    """Copy *source* to *dest* with ``/Title``/``/Author`` set.

    The write is incremental: the staged file is the source bytes plus
    an appended update, so malformed scans are never fully re-parsed or
    re-serialized and every structure (outlines, forms, page labels,
    XMP stream) survives byte-identical.  The existing ``/Info``
    dictionary is carried over with only the two fields replaced.
    The source is never opened for writing, and the destination is
    published atomically via a sibling temporary file.
    """
    from pypdf import PdfReader, PdfWriter

    target = Path(dest)
    if target.resolve() == Path(source).resolve():
        raise ValueError(f"source and destination are the same file: {source}")
    with open(source, "rb") as read_only:
        probe = PdfReader(read_only)
        if probe.is_encrypted:
            raise ValueError(f"encrypted PDF: {source}")
        existing = (
            {str(key): str(value) for key, value in dict(probe.metadata or {}).items()}
        )
    info = dict(existing)
    if title:
        info["/Title"] = title
    if authors:
        info["/Author"] = "; ".join(authors)
    writer = PdfWriter(str(source), incremental=True)
    if info:
        writer.add_metadata(info)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            writer.write(stream)
        os.replace(tmp_name, target)
    except BaseException:
        # Remove only the temp file this call created: it lives in the
        # destination directory under the unique mkstemp name, so anything
        # else at that path is not ours to delete.  After a successful
        # replace the temp name is gone and this is a no-op.
        try:
            candidate = Path(tmp_name)
            if (candidate.parent == target.parent
                    and candidate.name.startswith(target.name + ".")
                    and candidate.is_file()):
                candidate.unlink()
        except OSError:
            pass
        raise


def _opf_namespaces(opf_data: bytes) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    for _, (prefix, uri) in ET.iterparse(
        __import__("io").BytesIO(opf_data), events=("start-ns",),
    ):
        prefixes.setdefault(prefix, uri)
    return prefixes


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag.rsplit(":", 1)[-1]


def embed_epub_metadata(
    source: str | Path, dest: str | Path,
    title: str | None, authors: list[str],
) -> None:
    """Copy *source* EPUB to *dest* with OPF title/creators replaced.

    The output stays a valid EPUB: ``mimetype`` is written first and
    uncompressed, every other member keeps its relative order,
    compression method and metadata, and only the OPF package document
    is rewritten.  Existing ``dc:creator`` elements are replaced
    wholesale so stale contributors never linger beside the accepted
    authors.
    """
    with zipfile.ZipFile(os.fspath(source), "r") as archive:
        infos = archive.infolist()
        blobs = {info.filename: archive.read(info.filename) for info in infos}
    try:
        container = blobs["META-INF/container.xml"]
    except KeyError as exc:
        raise ValueError("EPUB is missing META-INF/container.xml") from exc
    root = _parse_container(container)
    opf_name = root
    try:
        opf_data = blobs[opf_name]
    except KeyError as exc:
        raise ValueError(f"EPUB is missing its OPF document {opf_name!r}") from exc
    for prefix, uri in _opf_namespaces(opf_data).items():
        try:
            ET.register_namespace(prefix, uri)
        except ValueError:
            pass
    package = ET.fromstring(opf_data)
    metadata = None
    if _local(package.tag) == "metadata":
        metadata = package
    else:
        for element in package.iter():
            if _local(element.tag) == "metadata":
                metadata = element
                break
    if metadata is None:
        raise ValueError("EPUB OPF has no metadata element")
    dc_ns = _DC_NS
    for element in list(metadata):
        if _local(element.tag) in ("title", "creator"):
            uri = element.tag[1:].rsplit("}", 1)[0] if element.tag.startswith("{") else ""
            if uri:
                dc_ns = uri
            break
    if title:
        title_el = None
        for element in list(metadata):
            if _local(element.tag) == "title":
                title_el = element
                break
        if title_el is None:
            title_el = ET.SubElement(metadata, f"{{{dc_ns}}}title")
        title_el.text = title
    if authors:
        for element in list(metadata):
            if _local(element.tag) == "creator":
                metadata.remove(element)
        for author in authors:
            creator = ET.Element(f"{{{dc_ns}}}creator")
            creator.text = author
            metadata.append(creator)
    blobs[opf_name] = ET.tostring(package, encoding="utf-8", xml_declaration=True)

    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    mime_info = next(
        (info for info in infos if info.filename == "mimetype"), None)
    mime_data = blobs.get("mimetype", b"application/epub+zip")
    with zipfile.ZipFile(tmp, "w") as archive:
        # The EPUB spec requires mimetype first and uncompressed.
        stamp = mime_info.date_time if mime_info is not None else None
        first = zipfile.ZipInfo("mimetype", date_time=stamp or (1980, 1, 1, 0, 0, 0))
        first.compress_type = zipfile.ZIP_STORED
        if mime_info is not None:
            first.external_attr = mime_info.external_attr
            first.create_system = mime_info.create_system
        archive.writestr(first, mime_data)
        # Every other member keeps its relative order, compression and
        # metadata; the OPF is rewritten in place at its original slot.
        for info in infos:
            if info.filename == "mimetype":
                continue
            data = blobs[opf_name] if info.filename == opf_name else blobs[info.filename]
            archive.writestr(info, data)
    os.replace(tmp, target)


def _parse_container(container: bytes) -> str:
    if b"<!DOCTYPE" in container or b"<!ENTITY" in container:
        raise ValueError("EPUB container declares forbidden markup")
    root = ET.fromstring(container)
    for element in root.iter():
        if _local(element.tag) != "rootfile":
            continue
        full_path = ""
        for key, value in element.attrib.items():
            if _local(key) == "full-path":
                full_path = (value or "").strip()
        normalized = posixpath.normpath(full_path.replace("\\", "/"))
        if (
            not normalized or normalized in (".", "..")
            or normalized.startswith(("/", "../")) or posixpath.isabs(full_path)
        ):
            raise ValueError("EPUB rootfile escapes the archive")
        return normalized
    raise ValueError("EPUB container names no OPF document")


def read_embedded(path: str | Path) -> dict[str, Any]:
    """Read back embedded title/authors with the catalogue extractors."""
    suffix = Path(str(path)).suffix.lower()
    if suffix == ".epub":
        return dict(extract_epub_metadata(path))
    return dict(extract_pdf_metadata(path))


# --- Plan workflow ---------------------------------------------------------


def _staged_name(source_sha: str, source: str) -> str:
    """Deterministic collision-free staged filename for one source path.

    The corpus has hundreds of duplicate-SHA groups whose paths carry
    *conflicting* accepted metadata, so the source hash alone cannot
    name the staged copy: two such records would overwrite each other's
    staged file and the loser would verify/apply the wrong metadata.
    The name therefore mixes the content hash (for human debugging)
    with a digest of the source path (for uniqueness).  The plan record
    keeps the full ``source_sha256`` separately, and ``apply_plan``
    still targets the original source path from the record key.
    """
    suffix = Path(source).suffix.lower() or ".bin"
    path_digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:16]
    return f"{source_sha or 'unknown'}-{path_digest}{suffix}"


def prepare_plan(
    manifest_path: str | Path,
    staging_dir: str | Path,
    plan_path: str | Path | None = None,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Stage embedded copies for manifest entries with accepted metadata.

    The source tree is only read.  Entries with status ``ok`` -- and
    ``ambiguous`` entries that still carry an accepted title or authors
    in their uncontested field -- are staged; entries with nothing
    accepted are recorded as skipped with a reason, keeping the final
    accounting explicit.  Returns counts including ``would_prepare``
    behavior under ``dry_run`` (nothing is written).
    """
    manifest = load_manifest(manifest_path)
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    resolved = (
        plan_path if plan_path is not None else staging / "embed-plan.json"
    )
    plan: dict[str, Any] = {"version": EMBED_TOOL_VERSION,
                            "staging_dir": str(staging), "entries": {}}
    if Path(resolved).exists():
        try:
            plan = json.load(open(resolved, encoding="utf-8"))
        except ValueError:
            plan = {"version": EMBED_TOOL_VERSION,
                    "staging_dir": str(staging), "entries": {}}
    records = plan.setdefault("entries", {})
    report = {"examined": 0, "prepared": 0, "reused": 0, "skipped": 0,
              "errors": 0}
    done = 0
    for key in sorted(manifest.get("entries", {})):
        if limit is not None and done >= limit:
            break
        entry = manifest["entries"][key]
        done += 1
        report["examined"] += 1
        title = entry.get("title")
        authors = list(entry.get("authors") or [])
        if (not title and not authors) or entry.get("status") not in ("ok", "ambiguous"):
            records[key] = {
                "status": f"skipped_{entry.get('status', 'empty')}",
                "reason": "no accepted metadata",
                "source_sha256": entry.get("sha256"),
            }
            report["skipped"] += 1
            continue
        source_sha = entry.get("sha256")
        staged_name = _staged_name(source_sha or "unknown", key)
        staged = str(staging / staged_name)
        previous = records.get(key)
        if (
            previous is not None and previous.get("status") == "prepared"
            and previous.get("source_sha256") == source_sha
            and previous.get("staged_sha256")
            and Path(previous.get("staged", staged)).exists()
            and sha256_of(previous["staged"]) == previous["staged_sha256"]
        ):
            report["reused"] += 1
            continue
        if dry_run:
            records[key] = {
                "status": "would_prepare", "source_sha256": source_sha,
                "staged": staged, "title": title, "authors": authors,
            }
            report["prepared"] += 1
            continue
        try:
            suffix = Path(key).suffix.lower()
            if suffix == ".epub":
                embed_epub_metadata(key, staged, title, authors)
            elif suffix == ".pdf":
                embed_pdf_metadata(key, staged, title, authors)
            else:
                raise ValueError(f"unsupported extension: {suffix}")
            staged_sha = sha256_of(staged)
            records[key] = {
                "status": "prepared", "source_sha256": source_sha,
                "staged": staged, "staged_sha256": staged_sha,
                "title": title, "authors": authors,
                "manifest_status": entry.get("status"),
            }
            report["prepared"] += 1
        except Exception as exc:
            records[key] = {
                "status": "error", "source_sha256": source_sha,
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }
            report["errors"] += 1
    if not dry_run:
        _atomic_write_json(resolved, plan)
    return report


def verify_plan(plan_path: str | Path) -> dict[str, Any]:
    """Re-read each staged copy and confirm the accepted values round-trip."""
    with open(plan_path, encoding="utf-8") as handle:
        plan = json.load(handle)
    report: dict[str, Any] = {"checked": 0, "verified": 0, "mismatch": 0,
                              "missing": 0, "details": {}}
    for key, record in plan.get("entries", {}).items():
        if record.get("status") != "prepared":
            continue
        report["checked"] += 1
        staged = record.get("staged")
        if staged is None or not Path(staged).exists():
            report["missing"] += 1
            report["details"][key] = "missing staged file"
            continue
        try:
            embedded = read_embedded(staged)
        except Exception as exc:
            report["mismatch"] += 1
            report["details"][key] = f"unreadable staged copy: {exc}"[:200]
            continue
        problems: list[str] = []
        if record.get("title") and embedded.get("title") != record["title"]:
            problems.append(
                f"title {embedded.get('title')!r} != {record['title']!r}")
        for author in record.get("authors") or []:
            if author not in list(embedded.get("authors") or []):
                problems.append(f"author {author!r} missing")
        if problems:
            report["mismatch"] += 1
            report["details"][key] = "; ".join(problems)[:300]
        else:
            report["verified"] += 1
    return report


def apply_plan(
    plan_path: str | Path, *, dry_run: bool = False,
) -> dict[str, int]:
    """Copy staged files over their sources with changed-source rejection.

    A source whose current hash differs from the recorded
    ``source_sha256`` is rejected, never overwritten.  Sources that
    already match the staged hash are skipped as already applied.
    Writes are atomic (temp file + ``os.replace``).
    """
    with open(plan_path, encoding="utf-8") as handle:
        plan = json.load(handle)
    report = {"examined": 0, "applied": 0, "already_applied": 0,
              "rejected_changed": 0, "missing": 0, "errors": 0}
    for key, record in plan.get("entries", {}).items():
        if record.get("status") != "prepared":
            continue
        report["examined"] += 1
        staged = record.get("staged")
        if staged is None or not Path(staged).exists():
            report["missing"] += 1
            continue
        try:
            current = sha256_of(key)
        except OSError:
            report["missing"] += 1
            continue
        if current == record.get("staged_sha256"):
            # The source already carries the staged bytes -- a previous
            # apply, or an identical file -- so there is nothing to do.
            # This check runs first so re-apply is a skip, not a rewrite.
            record["source_sha256"] = record["staged_sha256"]
            report["already_applied"] += 1
            continue
        if current != record.get("source_sha256"):
            report["rejected_changed"] += 1
            continue
        if dry_run:
            report["applied"] += 1
            continue
        try:
            if sha256_of(staged) != record.get("staged_sha256"):
                report["errors"] += 1
                continue
            _atomic_replace(staged, key)
            record["source_sha256"] = record["staged_sha256"]
            report["applied"] += 1
        except Exception:
            report["errors"] += 1
    if not dry_run:
        _atomic_write_json(plan_path, plan)
    return report


def status_report(plan_path: str | Path) -> dict[str, int]:
    """Summarize a plan without changing anything (recovery overview)."""
    with open(plan_path, encoding="utf-8") as handle:
        plan = json.load(handle)
    report = {"prepared": 0, "staged_ok": 0, "staged_missing": 0,
              "source_changed": 0, "already_applied": 0,
              "skipped": 0, "errors": 0}
    for key, record in plan.get("entries", {}).items():
        status = record.get("status")
        if status != "prepared":
            if status == "error":
                report["errors"] += 1
            else:
                report["skipped"] += 1
            continue
        report["prepared"] += 1
        staged = record.get("staged")
        if staged is None or not Path(staged).exists():
            report["staged_missing"] += 1
            continue
        report["staged_ok"] += 1
        try:
            current = sha256_of(key)
        except OSError:
            report["source_changed"] += 1
            continue
        if current == record.get("staged_sha256"):
            report["already_applied"] += 1
        elif current != record.get("source_sha256"):
            report["source_changed"] += 1
    return report


def _atomic_replace(src: str | Path, dest: str | Path) -> None:
    dest_path = Path(dest)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(dest_path.parent), prefix=dest_path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            with open(src, "rb") as handle:
                shutil.copyfileobj(handle, out)
        os.replace(tmp_name, dest_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_write_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, target)
