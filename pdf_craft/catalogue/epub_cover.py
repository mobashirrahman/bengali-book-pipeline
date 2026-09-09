"""Return the declared cover image of an EPUB, if it has one.

Read-only helper: it never raises and never writes.  Any failure -- a corrupt
archive, a missing OPF package document, a dangling manifest reference, an
unreadable member -- returns ``None``.
"""

from __future__ import annotations

import mimetypes
import re
import zipfile
from pathlib import Path, PurePosixPath

# Same first-match approach as catalogue.embed_metadata (duplicated here to keep
# this module dependency-free): the OPF is the first zip member ending in .opf.
_OPF_NAME = re.compile(r"\.opf$", re.IGNORECASE)

_ITEM_TAG = re.compile(r"<item\b[^>]*>", re.IGNORECASE | re.DOTALL)
_META_TAG = re.compile(r"<meta\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ATTR = re.compile(r"([\w:.-]+)\s*=\s*(['\"])(.*?)\2", re.DOTALL)


def _attrs(tag: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in _ATTR.finditer(tag):
        found[match.group(1).lower()] = match.group(3)
    return found


def _resolve_href(opf_name: str, href: str) -> str:
    """Resolve a manifest href relative to the OPF's own directory in the zip."""
    href = href.strip().split("#", 1)[0].split("?", 1)[0].strip()
    if not href:
        return ""
    if href.startswith("/"):
        # Root-relative inside the archive: no OPF directory to join.
        candidate = href.lstrip("/")
    else:
        parent = PurePosixPath(opf_name).parent
        if str(parent) in ("", "."):
            candidate = href
        else:
            candidate = (parent / href).as_posix()
    # Collapse "." and ".." segments lexically (PurePosixPath keeps ".." as-is).
    parts: list[str] = []
    for part in candidate.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def extract_epub_cover(path: str | Path) -> tuple[bytes, str] | None:
    """Return (image_bytes, mime_type) for an EPUB's declared cover image.

    Priority: EPUB3 ``cover-image`` manifest property first, then the EPUB2
    ``<meta name="cover">`` reference.  Returns ``None`` when there is no
    declared cover or anything about the lookup fails.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            opf_names = [name for name in names if _OPF_NAME.search(name)]
            if not opf_names:
                return None
            opf_name = opf_names[0]
            try:
                opf_xml = archive.read(opf_name).decode("utf-8", errors="replace")
            except KeyError:
                return None

            href: str | None = None

            # 1. EPUB3: manifest item whose space-separated properties list
            # contains the cover-image token.
            for tag in _ITEM_TAG.findall(opf_xml):
                attrs = _attrs(tag)
                properties = attrs.get("properties", "")
                if "cover-image" in properties.split():
                    candidate = attrs.get("href", "").strip()
                    if candidate:
                        href = candidate
                        break

            # 2. EPUB2: <meta name="cover" content="ID"/> pointing at a
            # manifest item id.
            if href is None:
                cover_id: str | None = None
                for tag in _META_TAG.findall(opf_xml):
                    attrs = _attrs(tag)
                    if attrs.get("name", "").lower() == "cover" and attrs.get("content", "").strip():
                        cover_id = attrs["content"].strip()
                        break
                if cover_id is not None:
                    for tag in _ITEM_TAG.findall(opf_xml):
                        attrs = _attrs(tag)
                        if attrs.get("id", "") == cover_id and attrs.get("href", "").strip():
                            href = attrs["href"].strip()
                            break
                    else:
                        return None

            if href is None:
                return None

            member = _resolve_href(opf_name, href)
            if not member:
                return None
            try:
                data = archive.read(member)
            except KeyError:
                return None
            mime, _ = mimetypes.guess_type(href)
            return bytes(data), mime or "application/octet-stream"
    except Exception:
        return None
