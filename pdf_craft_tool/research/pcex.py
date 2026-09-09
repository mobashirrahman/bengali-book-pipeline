"""Read region geometry from a ``.pcex`` OCR artifact (read-only).

A ``.pcex`` file is a zip archive produced by the OCR pipeline. ``pages.xml``
lists page pixel dimensions and the render DPI; ``chapters/*.xml`` hold
``<block page_index="N" order="K" det="x0,y0,x1,y1">text</block>`` elements in
the ``ocr_pixels`` coordinate space.

This module is used only to *seed* a draft :class:`~pdf_craft_tool.research.
census.PageCensus` for blind annotation. The seed is a starting point: a human
census review compares each page image against the seeded regions and adds any
the OCR dropped before annotation opens (roadmap S2). Nothing here is treated
as gold, and no OCR text is carried into the annotation payload.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

from pdf_craft_tool.research import schema

#: ``<paragraph ref="...">`` values that mark a heading rather than body text.
_HEADING_REFS = frozenset({"title", "sub_title", "headline", "head", "chapter"})


@dataclass(frozen=True)
class PcexPage:
    """One page's seed data: pixel size, render DPI and ordered regions."""

    page_number: int
    width: int
    height: int
    render_dpi: int
    regions: tuple

    def census_regions(self) -> list[dict]:
        """Region dicts ready for :func:`census.build_census_from_regions`.

        ``render_scale`` is stored as pixels-per-point (``dpi / 72``) so a
        later consumer can recover PDF points from the pixel geometry.
        """
        scale = self.render_dpi / 72.0
        out: list[dict] = []
        for index, region in enumerate(self.regions):
            x0 = max(0, int(region["x0"]))
            y0 = max(0, int(region["y0"]))
            x1 = max(x0 + 1, int(region["x1"]))
            y1 = max(y0 + 1, int(region["y1"]))
            out.append({
                "region_index": index,
                "geometry": {
                    "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "unit": "px",
                    "render_scale": scale,
                    "orientation": 0,
                },
                "kind": region["kind"],
                "unreadable": False,
                "note": region.get("note", ""),
            })
        if not out:
            # A page the OCR found no text on (blank leaf or a full-page
            # figure). Seed one page-sized region so the page still enters
            # annotation; census review decides what it really is.
            out.append({
                "region_index": 0,
                "geometry": {
                    "x0": 0, "y0": 0,
                    "x1": max(1, int(self.width)),
                    "y1": max(1, int(self.height)),
                    "unit": "px", "render_scale": scale, "orientation": 0,
                },
                "kind": "body",
                "unreadable": False,
                "note": "seed: OCR found no text blocks on this page; "
                        "census review required",
            })
        return out


def _parse_det(value: str):
    parts = [piece.strip() for piece in (value or "").split(",")]
    if len(parts) != 4:
        raise schema.ContractError(f"det must be 'x0,y0,x1,y1', got {value!r}")
    try:
        x0, y0, x1, y1 = (int(round(float(piece))) for piece in parts)
    except ValueError as exc:
        raise schema.ContractError(f"non-numeric det {value!r}") from exc
    if x1 < x0 or y1 < y0:
        raise schema.ContractError(f"det is not left-top to right-bottom: {value!r}")
    return x0, y0, x1, y1


def _kind_for(ref: str) -> str:
    return "heading" if (ref or "").strip().lower() in _HEADING_REFS else "body"


def read_pcex_pages(pcex_path, page_numbers) -> dict:
    """Return ``{page_number: PcexPage}`` for the requested pages.

    ``page_numbers`` are 1-indexed PDF page numbers (identical to the OCR
    ``page_index``). A requested page absent from ``pages.xml`` raises
    :class:`~pdf_craft_tool.research.schema.ContractError`.
    """
    path = Path(pcex_path)
    wanted = sorted({int(number) for number in page_numbers})
    if not wanted:
        raise schema.ContractError("page_numbers must be non-empty")
    if not zipfile.is_zipfile(path):
        raise schema.ContractError(f"{path} is not a .pcex zip archive")

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "pages.xml" not in names:
            raise schema.ContractError(f"{path} has no pages.xml")
        pages_root = ET.fromstring(archive.read("pages.xml"))
        render_dpi = int(pages_root.attrib.get("render_dpi", "300"))
        dims: dict[int, tuple] = {}
        for node in pages_root.findall("page"):
            dims[int(node.attrib["index"])] = (
                int(node.attrib["width"]), int(node.attrib["height"]))

        missing = [number for number in wanted if number not in dims]
        if missing:
            raise schema.ContractError(
                f"{path} pages.xml has no page(s) {missing}")

        # blocks per page, kept in (chapter order, block order) sequence.
        blocks: dict[int, list] = {number: [] for number in wanted}
        chapter_names = sorted(
            name for name in names
            if name.startswith("chapters/") and name.endswith(".xml"))
        for seq, name in enumerate(chapter_names):
            chapter_root = ET.fromstring(archive.read(name))
            for paragraph in chapter_root.iter("paragraph"):
                ref = paragraph.attrib.get("ref", "")
                for block in paragraph.findall("block"):
                    try:
                        page_index = int(block.attrib["page_index"])
                    except (KeyError, ValueError):
                        continue
                    if page_index not in blocks:
                        continue
                    try:
                        order = int(block.attrib.get("order", "0"))
                    except ValueError:
                        order = 0
                    x0, y0, x1, y1 = _parse_det(block.attrib.get("det", ""))
                    blocks[page_index].append(
                        (seq, order, x0, y0, x1, y1, _kind_for(ref)))

    result: dict[int, PcexPage] = {}
    for number in wanted:
        width, height = dims[number]
        ordered = sorted(blocks[number], key=lambda item: (item[0], item[1]))
        regions = tuple(
            {"x0": item[2], "y0": item[3], "x1": item[4], "y1": item[5],
             "kind": item[6]}
            for item in ordered)
        result[number] = PcexPage(
            page_number=number, width=width, height=height,
            render_dpi=render_dpi, regions=regions)
    return result
