"""Write resolved titles and authors back into the book files themselves.

The catalogue knows what each file is, but the file does not.  Copied onto a
reader, an e-ink device or another machine, a book leaves the catalogue behind
and shows up as ``Unknown Author - 7b7caf23f06a.pdf`` again.  Embedding the
metadata makes the identification travel with the file.

This module modifies the user's collection, so every write is built to fail
safe:

* PDFs are written **incrementally** -- pypdf appends only the changed objects
  and leaves the original bytes untouched, so the page content cannot be
  re-encoded, degraded or reordered by the write.
* Every write goes to a temporary file beside the target, is verified by being
  reopened and re-read, and only then replaces the original with an atomic
  ``os.replace``.  A failure anywhere leaves the original exactly as it was.
* The pre-write SHA-256 is returned for every file touched, so an audit trail
  can be recorded and the change traced.

Nothing here decides *what* to embed.  It is handed a title and authors that
some other stage has already resolved, and it refuses to write empty values.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

WRITTEN = "written"
SKIPPED = "skipped"
FAILED = "failed"

# Stamped into /Producer so a later pass can tell which files this tool has
# already touched without having to diff their metadata.
PRODUCER = "pdf-craft catalogue metadata"

_OPF_NAME = re.compile(r"\.opf$", re.IGNORECASE)


@dataclass(frozen=True)
class EmbedResult:
    path: str
    status: str
    reason: str = ""
    sha256_before: str = ""

    @property
    def ok(self) -> bool:
        return self.status == WRITTEN


def _sha256(path: str | Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _authors_string(authors) -> str:
    """PDF ``/Author`` is a single string; the conventional join is a comma."""
    return ", ".join(name.strip() for name in authors if name and name.strip())


def embed_pdf(
    path: str | Path,
    *,
    title: str = "",
    authors=(),
    dry_run: bool = False,
) -> EmbedResult:
    """Set ``/Title`` and ``/Author`` on a PDF without rewriting its pages."""
    from pypdf import PdfWriter

    path = Path(path)
    author = _authors_string(authors)
    if not title.strip() and not author:
        return EmbedResult(str(path), SKIPPED, "nothing to write")
    if not path.is_file():
        return EmbedResult(str(path), FAILED, "missing")

    try:
        before = _sha256(path)
    except OSError as error:
        return EmbedResult(str(path), FAILED, f"unreadable: {error}")
    if dry_run:
        return EmbedResult(str(path), SKIPPED, "dry run", before)

    fields: dict[str, str] = {"/Producer": PRODUCER}
    if title.strip():
        fields["/Title"] = title.strip()
    if author:
        fields["/Author"] = author

    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(handle)
    try:
        # incremental=True appends a small update section rather than
        # regenerating the document, which is what keeps a 40MB scan from being
        # re-encoded just to record its title.
        # The source must be the positional argument here: with
        # incremental=True pypdf reopens *fileobj* to copy the original bytes,
        # and clone_from would leave it empty.
        writer = PdfWriter(str(path), incremental=True)
        writer.add_metadata(fields)
        with open(temporary, "wb") as target:
            writer.write(target)
            target.flush()
            os.fsync(target.fileno())

        # Verify before replacing: a file that will not reopen, has lost pages,
        # or did not take the metadata must never overwrite the original.
        from pypdf import PdfReader

        check = PdfReader(temporary)
        original_pages = len(PdfReader(str(path)).pages)
        if len(check.pages) != original_pages:
            raise ValueError("page count changed")
        written = check.metadata or {}
        if title.strip() and (written.get("/Title") or "") != title.strip():
            raise ValueError("title did not persist")

        os.replace(temporary, path)
        return EmbedResult(str(path), WRITTEN, "", before)
    except Exception as error:  # noqa: BLE001 - any failure must leave the original
        return EmbedResult(str(path), FAILED, f"{type(error).__name__}: {error}", before)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _rewrite_opf(xml: str, title: str, authors) -> str:
    """Replace dc:title and dc:creator in an OPF metadata block."""
    if title.strip():
        if re.search(r"<dc:title[^>]*>", xml):
            xml = re.sub(
                r"(<dc:title[^>]*>).*?(</dc:title>)",
                lambda match: match.group(1) + _escape(title.strip()) + match.group(2),
                xml, count=1, flags=re.DOTALL,
            )
        else:
            xml = _insert_into_metadata(xml, f"<dc:title>{_escape(title.strip())}</dc:title>")

    names = [name.strip() for name in authors if name and name.strip()]
    if names:
        # Existing creators are removed wholesale rather than edited in place:
        # a file may carry several, and a partial overwrite would leave a
        # mixture of old and new attributions.
        xml = re.sub(r"\s*<dc:creator[^>]*>.*?</dc:creator>", "", xml, flags=re.DOTALL)
        xml = re.sub(r"\s*<dc:creator[^>]*/>", "", xml)
        block = "".join(f"<dc:creator>{_escape(name)}</dc:creator>" for name in names)
        xml = _insert_into_metadata(xml, block)
    return xml


def _escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _insert_into_metadata(xml: str, fragment: str) -> str:
    match = re.search(r"</metadata>", xml, re.IGNORECASE)
    if not match:
        return xml
    return xml[:match.start()] + fragment + xml[match.start():]


def embed_epub(
    path: str | Path,
    *,
    title: str = "",
    authors=(),
    dry_run: bool = False,
) -> EmbedResult:
    """Set ``dc:title`` and ``dc:creator`` in an EPUB's OPF package document."""
    path = Path(path)
    names = [name.strip() for name in authors if name and name.strip()]
    if not title.strip() and not names:
        return EmbedResult(str(path), SKIPPED, "nothing to write")
    if not path.is_file():
        return EmbedResult(str(path), FAILED, "missing")

    try:
        before = _sha256(path)
    except OSError as error:
        return EmbedResult(str(path), FAILED, f"unreadable: {error}")
    if dry_run:
        return EmbedResult(str(path), SKIPPED, "dry run", before)

    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(handle)
    try:
        with zipfile.ZipFile(path) as source:
            opf_names = [name for name in source.namelist() if _OPF_NAME.search(name)]
            if not opf_names:
                raise ValueError("no OPF package document")
            entries = source.infolist()
            payload = {info.filename: source.read(info.filename) for info in entries}

        opf_name = opf_names[0]
        payload[opf_name] = _rewrite_opf(
            payload[opf_name].decode("utf-8", errors="replace"), title, names
        ).encode("utf-8")

        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target:
            # The EPUB specification requires "mimetype" to be the first entry
            # and stored uncompressed; writing it any other way produces an
            # archive that readers reject outright.
            if "mimetype" in payload:
                info = zipfile.ZipInfo("mimetype")
                info.compress_type = zipfile.ZIP_STORED
                target.writestr(info, payload["mimetype"])
            for info in entries:
                if info.filename == "mimetype":
                    continue
                target.writestr(info.filename, payload[info.filename])

        with zipfile.ZipFile(temporary) as check:
            if check.testzip() is not None:
                raise ValueError("rewritten archive is corrupt")
            if len(check.namelist()) != len(entries):
                raise ValueError("entry count changed")
            rewritten = check.read(opf_name).decode("utf-8", errors="replace")
        if title.strip() and _escape(title.strip()) not in rewritten:
            raise ValueError("title did not persist")

        os.replace(temporary, path)
        return EmbedResult(str(path), WRITTEN, "", before)
    except Exception as error:  # noqa: BLE001 - any failure must leave the original
        return EmbedResult(str(path), FAILED, f"{type(error).__name__}: {error}", before)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def embed_document(
    path: str | Path,
    media_type: str,
    *,
    title: str = "",
    authors=(),
    dry_run: bool = False,
) -> EmbedResult:
    if media_type == "application/epub+zip":
        return embed_epub(path, title=title, authors=authors, dry_run=dry_run)
    return embed_pdf(path, title=title, authors=authors, dry_run=dry_run)
