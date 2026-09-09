"""Full-page census records for blind annotation (S2).

A census enumerates every region on a page image independently of any OCR
output, so text the OCR dropped is still captured and can still become gold.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from pdf_craft_tool.research import schema

#: Census entries of these kinds carry transcribed text and are checked for
#: OCR omissions. Other kinds (folio, figure, ...) are structural.
TEXT_REGION_KINDS = frozenset({"body", "heading", "verse", "footnote"})


class RegionKind(enum.Enum):
    BODY = "body"
    HEADING = "heading"
    RUNNING_HEAD = "running_head"
    FOLIO = "folio"
    FOOTNOTE = "footnote"
    MARGINALIA = "marginalia"
    CAPTION = "caption"
    CATCHWORD = "catchword"
    TABLE_ROW = "table_row"
    FIGURE = "figure"
    VERSE = "verse"


_REGION_KIND_VALUES = frozenset(member.value for member in RegionKind)

_CENSUS_ENTRY_KEYS = frozenset(
    {"region_index", "geometry", "kind", "unreadable", "note"}
)

_PAGE_CENSUS_KEYS = frozenset(
    {
        "page_id",
        "source_sha256",
        "image_sha256",
        "entries",
        "reading_order",
        "annotator_id",
        "guideline_version",
    }
)


def _require_region_index(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise schema.ContractError(
            f"region_index must be a non-negative int, got {value!r}"
        )
    return value


@dataclass(frozen=True)
class CensusEntry:
    region_index: int
    geometry: schema.Geometry
    kind: str
    unreadable: bool
    note: str = ""

    def __post_init__(self):
        object.__setattr__(self, "region_index",
                            _require_region_index(self.region_index))
        geometry = self.geometry
        if isinstance(geometry, dict):
            geometry = schema.Geometry.from_dict(geometry)
        if not isinstance(geometry, schema.Geometry):
            raise schema.ContractError(
                f"geometry must be a Geometry, got {self.geometry!r}"
            )
        object.__setattr__(self, "geometry", geometry)
        if not isinstance(self.kind, str) or self.kind not in _REGION_KIND_VALUES:
            raise schema.ContractError(
                f"kind must be one of {sorted(_REGION_KIND_VALUES)}, "
                f"got {self.kind!r}"
            )
        if not isinstance(self.unreadable, bool):
            raise schema.ContractError(
                f"unreadable must be a bool, got {self.unreadable!r}"
            )
        if not isinstance(self.note, str):
            raise schema.ContractError(
                f"note must be a string, got {self.note!r}"
            )

    def to_dict(self) -> dict:
        return {
            "region_index": self.region_index,
            "geometry": self.geometry.to_dict(),
            "kind": self.kind,
            "unreadable": self.unreadable,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CensusEntry":
        if not isinstance(data, dict):
            raise schema.ContractError(
                f"CensusEntry must be built from a dict, got {type(data)}"
            )
        unknown = set(data) - _CENSUS_ENTRY_KEYS
        if unknown:
            raise schema.ContractError(
                f"unknown keys for CensusEntry: {sorted(unknown)}"
            )
        try:
            return cls(
                region_index=data["region_index"],
                geometry=data["geometry"],
                kind=data["kind"],
                unreadable=data["unreadable"],
                note=data.get("note", ""),
            )
        except KeyError as exc:
            raise schema.ContractError(
                f"CensusEntry missing required key {exc}"
            ) from exc


def _check_dense(entries: tuple) -> None:
    indices = [entry.region_index for entry in entries]
    if len(set(indices)) != len(indices):
        raise schema.ContractError("CensusEntry region_index values must be unique")
    if set(indices) != set(range(len(entries))):
        raise schema.ContractError(
            "CensusEntry region_index values must be dense 0..n-1, "
            f"got {sorted(indices)}"
        )


def _check_reading_order(reading_order: tuple, count: int) -> None:
    for value in reading_order:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise schema.ContractError(
                f"reading_order entries must be non-negative ints, got {value!r}"
            )
    if sorted(reading_order) != list(range(count)):
        raise schema.ContractError(
            "reading_order must be a permutation of the region indices"
        )


@dataclass(frozen=True)
class PageCensus:
    page_id: str
    source_sha256: str
    image_sha256: str
    entries: tuple
    reading_order: tuple
    annotator_id: str
    guideline_version: str = "annot-1"

    def __post_init__(self):
        if not schema._is_sha256(self.page_id):
            raise schema.ContractError(
                "page_id must be a 64-char lowercase sha256 hex string"
            )
        if not schema._is_sha256(self.source_sha256):
            raise schema.ContractError(
                "source_sha256 must be a 64-char lowercase sha256 hex string"
            )
        if not schema._is_sha256(self.image_sha256):
            raise schema.ContractError(
                "image_sha256 must be a 64-char lowercase sha256 hex string"
            )
        raw_entries = self.entries
        if isinstance(raw_entries, list):
            raw_entries = tuple(raw_entries)
        if not isinstance(raw_entries, tuple):
            raise schema.ContractError(
                f"entries must be a list or tuple, got {type(self.entries)}"
            )
        parsed = []
        for entry in raw_entries:
            if isinstance(entry, dict):
                entry = CensusEntry.from_dict(entry)
            if not isinstance(entry, CensusEntry):
                raise schema.ContractError(
                    f"entries must be CensusEntry, got {entry!r}"
                )
            parsed.append(entry)
        object.__setattr__(self, "entries", tuple(parsed))
        _check_dense(self.entries)
        raw_order = self.reading_order
        if isinstance(raw_order, list):
            raw_order = tuple(raw_order)
        if not isinstance(raw_order, tuple):
            raise schema.ContractError(
                f"reading_order must be a list or tuple, got {type(self.reading_order)}"
            )
        object.__setattr__(self, "reading_order", tuple(raw_order))
        _check_reading_order(self.reading_order, len(self.entries))
        if not isinstance(self.annotator_id, str) or not self.annotator_id:
            raise schema.ContractError(
                f"annotator_id must be a non-empty string, got {self.annotator_id!r}"
            )
        if not isinstance(self.guideline_version, str) or not self.guideline_version:
            raise schema.ContractError(
                "guideline_version must be a non-empty string, "
                f"got {self.guideline_version!r}"
            )

    def to_dict(self) -> dict:
        return {
            "page_id": self.page_id,
            "source_sha256": self.source_sha256,
            "image_sha256": self.image_sha256,
            "entries": [entry.to_dict() for entry in self.entries],
            "reading_order": list(self.reading_order),
            "annotator_id": self.annotator_id,
            "guideline_version": self.guideline_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PageCensus":
        if not isinstance(data, dict):
            raise schema.ContractError(
                f"PageCensus must be built from a dict, got {type(data)}"
            )
        unknown = set(data) - _PAGE_CENSUS_KEYS
        if unknown:
            raise schema.ContractError(
                f"unknown keys for PageCensus: {sorted(unknown)}"
            )
        try:
            kwargs = {key: data[key] for key in _PAGE_CENSUS_KEYS if key in data}
            if "guideline_version" not in kwargs:
                kwargs["guideline_version"] = "annot-1"
            return cls(**kwargs)
        except KeyError as exc:
            raise schema.ContractError(
                f"PageCensus missing required key {exc}"
            ) from exc


def build_census_from_regions(
    page_id: str,
    source_sha256: str,
    image_sha256: str,
    annotator_id: str,
    regions: list[dict],
    *,
    guideline_version: str = "annot-1",
    reading_order=None,
) -> PageCensus:
    """Build a validated :class:`PageCensus` from raw region dicts.

    Each region dict carries ``geometry`` (a ``Geometry`` dict), ``kind``,
    ``unreadable`` and an optional ``note``. ``region_index`` values must be
    dense ``0..n-1``; when omitted from every region they are assigned in
    input order. The default reading order follows the input order.
    """
    if not isinstance(regions, (list, tuple)) or not regions:
        raise schema.ContractError("regions must be a non-empty list of dicts")
    indexed = []
    for position, region in enumerate(regions):
        if not isinstance(region, dict):
            raise schema.ContractError(
                f"regions[{position}] must be a dict, got {type(region)}"
            )
        payload = dict(region)
        if "region_index" not in payload:
            if any("region_index" in item for item in regions
                   if isinstance(item, dict)):
                raise schema.ContractError(
                    "regions must either all carry region_index or none do"
                )
            payload["region_index"] = position
        indexed.append(payload)
    entries = tuple(CensusEntry.from_dict(item) for item in indexed)
    _check_dense(entries)
    if reading_order is None:
        order = tuple(item["region_index"] for item in indexed)
    else:
        order = tuple(reading_order)
    return PageCensus(
        page_id=page_id,
        source_sha256=source_sha256,
        image_sha256=image_sha256,
        entries=entries,
        reading_order=order,
        annotator_id=annotator_id,
        guideline_version=guideline_version,
    )


def _bbox_iou(first: list, second: list) -> float:
    inter_x0 = max(first[0], second[0])
    inter_y0 = max(first[1], second[1])
    inter_x1 = min(first[2], second[2])
    inter_y1 = min(first[3], second[3])
    inter = max(0.0, inter_x1 - inter_x0) * max(0.0, inter_y1 - inter_y0)
    if inter <= 0:
        return 0.0
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = first_area + second_area - inter
    if union <= 0:
        return 0.0
    return inter / union


def census_omission_check(census: PageCensus, ocr_lines: list[dict]) -> dict:
    """Report census text regions the OCR output fails to cover.

    ``ocr_lines`` are ``{"text": str, "bbox": [x0, y0, x1, y1]}`` dicts from
    OCR or a model prediction. Each non-unreadable ``body``/``heading``/
    ``verse``/``footnote`` census entry matches an OCR line when bbox IoU is
    >= 0.3. An OCR pass that dropped a line yields ``omitted_lines >= 1``,
    while the missed entry stays in the census (and therefore in gold).
    """
    if not isinstance(census, PageCensus):
        raise schema.ContractError(
            f"census must be a PageCensus, got {type(census)}"
        )
    if not isinstance(ocr_lines, (list, tuple)):
        raise schema.ContractError("ocr_lines must be a list of dicts")
    boxes = []
    for position, line in enumerate(ocr_lines):
        if not isinstance(line, dict):
            raise schema.ContractError(
                f"ocr_lines[{position}] must be a dict, got {type(line)}"
            )
        bbox = line.get("bbox")
        if (
            not isinstance(bbox, (list, tuple))
            or len(bbox) != 4
            or not all(isinstance(value, (int, float))
                       and not isinstance(value, bool) for value in bbox)
        ):
            raise schema.ContractError(
                f"ocr_lines[{position}]['bbox'] must be [x0, y0, x1, y1] numbers"
            )
        boxes.append([float(value) for value in bbox])
    text_entries = [
        entry for entry in census.entries
        if entry.kind in TEXT_REGION_KINDS and not entry.unreadable
    ]
    omitted = []
    for entry in text_entries:
        geometry = entry.geometry
        entry_box = [float(geometry.x0), float(geometry.y0),
                     float(geometry.x1), float(geometry.y1)]
        matched = any(_bbox_iou(entry_box, box) >= 0.3 for box in boxes)
        if not matched:
            omitted.append(entry.region_index)
    total = len(text_entries)
    matched_count = total - len(omitted)
    return {
        "census_text_regions": total,
        "matched": matched_count,
        "omitted_lines": len(omitted),
        "omitted_region_index": sorted(omitted),
    }
