"""EPUB-embedded Dublin Core metadata extraction for catalogue matching.

The matcher in :mod:`pdf_craft.catalogue.resolution` falls back to guessing
from file paths when ``metadata_json`` is empty, so this module fills that
dict from the EPUB files themselves.  It parses the OPF package document
with the standard library only and never guesses: absent fields are omitted.
"""

from __future__ import annotations

import os
import posixpath
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .isbn import normalize_isbn

# Container and OPF documents are small descriptors; anything larger is either
# corrupt or a zip bomb trying to exhaust memory during a bulk backfill.
_MAX_MEMBER_BYTES = 4 * 1024 * 1024
_CONTAINER_PATH = "META-INF/container.xml"


class EpubMetadataError(ValueError):
    """Raised when an EPUB cannot be opened or its metadata cannot be parsed."""


def extract_epub_metadata(path: str | Path) -> dict[str, object]:
    """Return title/authors/isbns plus optional language/publisher/date.

    Every returned field also records its OPF source in ``provenance``.  Files
    with no usable metadata yield an empty dict rather than placeholders.
    """
    opf_data = _load_opf(path)
    metadata = _metadata_element(opf_data)
    if metadata is None:
        return {}
    result: dict[str, object] = {}
    provenance: dict[str, str] = {}

    title = _first_text(metadata, "title")
    if title:
        result["title"] = title
        provenance["title"] = "opf:dc:title"

    authors = _authors(metadata)
    if authors:
        result["authors"] = authors
        provenance["authors"] = "opf:dc:creator"

    isbns = _isbns(metadata)
    if isbns:
        result["isbns"] = isbns
        provenance["isbns"] = "opf:dc:identifier"

    for key in ("language", "publisher", "date"):
        value = _first_text(metadata, key)
        if value:
            result[key] = value
            provenance[key] = f"opf:dc:{key}"

    if provenance:
        result["provenance"] = provenance
    return result


def _load_opf(path: str | Path) -> bytes:
    """Return the OPF bytes named by the EPUB container document."""
    try:
        with zipfile.ZipFile(os.fspath(path), "r") as archive:
            container = _read_member(archive, _CONTAINER_PATH)
            return _read_member(archive, _opf_path(container))
    except EpubMetadataError:
        raise
    except Exception as exc:
        # A bulk backfill loops over thousands of untrusted files, so every
        # zip error surfaces as the single dedicated type, never raw.
        raise EpubMetadataError(f"cannot open EPUB: {exc}") from exc


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    """Read one zip member with a cap against decompression bombs."""
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise EpubMetadataError(f"EPUB is missing {name}") from exc
    if info.file_size > _MAX_MEMBER_BYTES or info.is_dir():
        raise EpubMetadataError(f"EPUB member {name!r} has an unsafe size")
    try:
        with archive.open(info, "r") as handle:
            data = handle.read(_MAX_MEMBER_BYTES + 1)
    except Exception as exc:
        raise EpubMetadataError(f"cannot read EPUB member {name!r}: {exc}") from exc
    if len(data) > _MAX_MEMBER_BYTES:
        raise EpubMetadataError(f"EPUB member {name!r} exceeds size limit")
    return data


def _opf_path(container: bytes) -> str:
    """Return the first rootfile path, rejecting traversal outside the zip."""
    root = _parse_xml(container, _CONTAINER_PATH)
    for element in root.iter():
        if _local(element.tag) != "rootfile":
            continue
        full_path = (element.get("full-path") or "").strip()
        if not full_path:
            continue
        normalized = posixpath.normpath(full_path.replace("\\", "/"))
        if (
            normalized in (".", "..")
            or normalized.startswith(("/", "../"))
            or posixpath.isabs(full_path)
            or _is_windows_absolute(full_path)
        ):
            raise EpubMetadataError("EPUB rootfile escapes the archive")
        return normalized
    raise EpubMetadataError("EPUB container names no OPF document")


def _is_windows_absolute(value: str) -> bool:
    return len(value) > 2 and value[1] == ":" and value[2] in ("/", "\\")


def _parse_xml(data: bytes, member: str) -> ET.Element:
    """Parse XML that must be free of entity declarations."""
    # ElementTree never fetches external entities, but internal ones still
    # expand in memory, so any DTD is refused before parsing untrusted bytes.
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise EpubMetadataError(f"EPUB member {member!r} declares forbidden markup")
    try:
        return ET.fromstring(data)
    except Exception as exc:
        raise EpubMetadataError(
            f"EPUB member {member!r} is not valid XML: {exc}"
        ) from exc


def _metadata_element(opf_data: bytes) -> ET.Element | None:
    root = _parse_xml(opf_data, "opf")
    if _local(root.tag) == "metadata":
        return root
    for element in root.iter():
        if _local(element.tag) == "metadata":
            return element
    return None


def _local(tag: str) -> str:
    """Return the element name without its namespace URI or prefix."""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag.rsplit(":", 1)[-1]


def _element_text(element: ET.Element) -> str:
    return "".join(element.itertext()).strip()


def _first_text(metadata: ET.Element, name: str) -> str | None:
    for element in metadata:
        if _local(element.tag) == name:
            text = _element_text(element)
            if text:
                return text
    return None


def _attribute(element: ET.Element, name: str) -> str | None:
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return None


def _authors(metadata: ET.Element) -> list[str]:
    """Return author-marked creators, or all creators when roles are absent.

    EPUB 2 marks the role on the creator itself while EPUB 3 refines it from
    a sibling ``meta`` element; both spellings feed the same filter so that
    editors and translators never pollute author matching.
    """
    roles_by_id: dict[str, str] = {}
    for element in metadata:
        if _local(element.tag) != "meta":
            continue
        refines = _attribute(element, "refines") or ""
        prop = _attribute(element, "property") or ""
        if (
            not refines.startswith("#")
            or prop.rsplit(":", 1)[-1].strip().lower() != "role"
        ):
            continue
        text = _element_text(element).lower()
        if text:
            roles_by_id.setdefault(refines[1:], text)
    entries: list[tuple[str, str | None]] = []
    for element in metadata:
        if _local(element.tag) != "creator":
            continue
        name = _element_text(element)
        if not name:
            continue
        role = _attribute(element, "role")
        if role is None:
            creator_id = _attribute(element, "id")
            role = roles_by_id.get(creator_id) if creator_id else None
        entries.append((name, role.strip().lower() if role and role.strip() else None))
    aut = [name for name, role in entries if role == "aut"]
    if aut:
        return aut
    if any(role is not None for _, role in entries):
        # Contributors are explicitly typed but none is an author; the
        # untyped names are the only plausible authors, if any remain.
        return [name for name, role in entries if role is None]
    return [name for name, _ in entries]


def _isbns(metadata: ET.Element) -> list[str]:
    """Return normalized ISBNs, dropping UUIDs and other non-ISBN identifiers."""
    isbns: list[str] = []
    for element in metadata:
        if _local(element.tag) != "identifier":
            continue
        text = _element_text(element)
        if not text:
            continue
        candidate = text
        if candidate.lower().startswith("urn:isbn:"):
            candidate = candidate[len("urn:isbn:") :].strip()
        try:
            normalized = normalize_isbn(candidate)
        except ValueError:
            continue
        if normalized not in isbns:
            isbns.append(normalized)
    return isbns
