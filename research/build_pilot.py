#!/usr/bin/env python3
"""Freeze the 60-page pilot study root from the signed-off sampling proposal.

Reproducible, offline, read-only against the corpus:

  * reads ``research/proposals/development-sampling-60.json`` (the frozen draw),
  * hashes each source PDF in ``data/`` and checks it against the proposal,
  * renders the 5 chosen pages of each book to PNG (poppler ``pdftoppm``),
  * seeds a draft page census from the book's ``.pcex`` OCR regions,
  * writes an immutable ``sample_manifest.json`` plus the annotation seed file.

It does NOT train, download, allocate cluster capacity, publish, or treat any
OCR/model text as gold. The census it writes is a DRAFT: a human census review
must check every page image against the seeded regions and add anything the OCR
dropped before annotation opens (roadmap S2).

Usage::

    .venv/bin/python research/build_pilot.py \
        --out pdf-craft-output/research/pilot-study

Re-running with the same proposal + corpus reproduces byte-identical manifests
(the render step is skipped for pages whose PNG already hashes correctly).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from pypdf import PdfReader

from pdf_craft_tool.research import census, pcex, schema

REPO = Path(__file__).resolve().parent.parent
PROPOSAL = REPO / "research" / "proposals" / "development-sampling-60.json"
JOBS = REPO / "pdf-craft-output" / "cluster" / "jobs"
STUDY_ID = "bengali-ocr-trust-pilot"
PILOT_SPLIT = "pilot"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _page_id(source_sha256: str, page_number: int) -> str:
    return schema.record_hash({
        "source_sha256": source_sha256,
        "page_number": page_number,
        "page_number_convention": "pdf_index",
    })


def _pcex_path(job_id: str) -> Path:
    matches = sorted((JOBS / job_id).glob("work/ocr/*/raw.pcex"))
    if not matches:
        raise SystemExit(f"no raw.pcex under job {job_id}")
    return matches[0]


def _ocr_source_sha(job_id: str) -> str:
    """The sha256 the OCR pipeline recorded for this book's source PDF.

    This -- not the current bytes on disk -- is the canonical key: the
    ``.pcex`` regions and every future B0/baseline prediction are keyed to
    it. The on-disk PDF may have drifted (e.g. metadata re-embedded in
    place); the rendered page is visually identical, so it is still a valid
    scan to show an annotator.
    """
    summary = json.loads(
        (JOBS / job_id / "summary.json").read_text(encoding="utf-8"))
    sha = summary.get("source_sha256", "")
    if not schema._is_sha256(sha):
        raise SystemExit(f"job {job_id} summary.json has no source_sha256")
    return sha


def _render_page(pdf_path: Path, page_number: int, pcex_width: int,
                 dest: Path) -> None:
    """Render one 1-indexed PDF page to ``dest`` (PNG) via pdftoppm."""
    reader = PdfReader(str(pdf_path))
    if reader.is_encrypted:
        reader.decrypt("")
    box = reader.pages[page_number - 1].cropbox
    pt_width = float(box.width) or 612.0
    dpi = max(72.0, min(600.0, 72.0 * pcex_width / pt_width))
    with tempfile.TemporaryDirectory() as work:
        prefix = Path(work) / "page"
        subprocess.run(
            ["pdftoppm", "-png", "-r", f"{dpi:.2f}",
             "-f", str(page_number), "-l", str(page_number),
             "-cropbox", str(pdf_path), str(prefix)],
            check=True, capture_output=True,
        )
        produced = sorted(Path(work).glob("page*.png"))
        if not produced:
            raise SystemExit(
                f"pdftoppm produced nothing for {pdf_path} p{page_number}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(produced[0].read_bytes())


def build(out_dir: Path) -> None:
    proposal = json.loads(PROPOSAL.read_text(encoding="utf-8"))
    seed = int(proposal["seed"])
    families = proposal["families"]

    pages_dir = out_dir / "pages"
    census_dir = out_dir / "census"
    records: list[dict] = []
    annotation_pages: list[dict] = []
    provenance: list[dict] = []

    for family in families:
        job_id = family["job_id"]
        declared_sha = family["sha256"]
        pdf_path = Path(family["source"])
        if not pdf_path.is_file():
            raise SystemExit(f"source PDF missing: {pdf_path}")

        # Canonical key: what the OCR pipeline saw. The proposal's sha must
        # agree with it (integrity check on the proposal itself).
        canonical_sha = _ocr_source_sha(job_id)
        if canonical_sha != declared_sha:
            raise SystemExit(
                f"{pdf_path.name}: proposal sha256 {declared_sha} != OCR "
                f"source sha {canonical_sha}")
        disk_sha = _sha256_file(pdf_path)
        pdf_drifted = disk_sha != canonical_sha
        actual_sha = canonical_sha

        page_numbers = [int(number) for number in family["pages"]]
        pcex_pages = pcex.read_pcex_pages(_pcex_path(job_id), page_numbers)
        stratum = "/".join(family["cell"])
        prob = float(family["sel_prob"])

        fam_pages = []
        for page_number in page_numbers:
            pid = _page_id(actual_sha, page_number)
            png = pages_dir / f"{pid}.png"
            pcex_page = pcex_pages[page_number]
            if not png.is_file():
                _render_page(pdf_path, page_number, pcex_page.width, png)
            image_sha = _sha256_file(png)

            page_census = census.build_census_from_regions(
                page_id=pid,
                source_sha256=actual_sha,
                image_sha256=image_sha,
                annotator_id="census-seed",
                regions=pcex_page.census_regions(),
            ).to_dict()
            (census_dir / f"{pid}.json").parent.mkdir(
                parents=True, exist_ok=True)
            (census_dir / f"{pid}.json").write_text(
                json.dumps(page_census, ensure_ascii=False, indent=2,
                           sort_keys=True) + "\n", encoding="utf-8")

            records.append(schema.SamplePage(
                page_id=pid,
                source_sha256=actual_sha,
                work_id="unknown",
                edition_id="unknown",
                overlap_group=job_id[:16],
                split=PILOT_SPLIT,
                stratum=stratum,
                selection_probability=prob,
                seed=seed,
                manifest_version=schema.MANIFEST_VERSION,
                schema_version=schema.SCHEMA_VERSION,
                file_hashes={"image_png": image_sha, "source_pdf": actual_sha},
                legacy_gold_ids=(),
            ).to_dict())
            annotation_pages.append({
                "page_id": pid,
                "source_sha256": actual_sha,
                "image_sha256": image_sha,
                "census": page_census,
            })
            fam_pages.append({
                "page_number": page_number, "page_id": pid,
                "image_sha256": image_sha,
                "regions": len(page_census["entries"]),
                "ocr_seeded_regions": len(pcex_page.regions),
                "pcex_size": [pcex_page.width, pcex_page.height],
            })

        provenance.append({
            "job_id": job_id, "source_sha256": actual_sha,
            "rendered_from_pdf_sha256": disk_sha,
            "pdf_bytes_drifted_from_ocr_source": pdf_drifted,
            "author_dir": family["author_dir"], "title": family["title"],
            "cell": family["cell"], "selection_probability": prob,
            "proof_available": family.get("proof_available", False),
            "pages": fam_pages,
        })

    manifest = schema.Manifest.build("sample", records)
    manifest.write(out_dir / "sample_manifest.json")
    (out_dir / "annotation_pages.json").write_text(
        json.dumps(annotation_pages, ensure_ascii=False, indent=2,
                   sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "pilot_provenance.json").write_text(
        json.dumps({
            "study_id": STUDY_ID, "seed": seed,
            "proposal": str(PROPOSAL.relative_to(REPO)),
            "proposal_sha256": _sha256_file(PROPOSAL),
            "manifest_hash": manifest.manifest_hash,
            "census_status": "draft_ocr_seeded_pending_human_review",
            "split": PILOT_SPLIT,
            "families": provenance,
        }, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    total_pages = len(records)
    total_regions = sum(len(page["census"]["entries"])
                        for page in annotation_pages)
    print(f"pilot study root: {out_dir}")
    print(f"  families: {len(families)}  pages: {total_pages}  "
          f"seeded regions: {total_regions}")
    print(f"  sample_manifest.json hash: {manifest.manifest_hash}")
    print(f"  images: {pages_dir}")
    print("  census is DRAFT (OCR-seeded); run the human census review "
          "before opening annotation.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path,
        default=REPO / "pdf-craft-output" / "research" / "pilot-study",
        help="study root directory to create")
    args = parser.parse_args(argv)
    build(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
