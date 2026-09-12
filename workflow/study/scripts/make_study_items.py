"""Build study voter items [{id, image}] from ocr_pages.json + pages/ dir.

Reads ``<study-root>/ocr_pages.json`` (``{page_id, image_ref, ...}`` rows)
and resolves each page to ``pages/<page_id>.png`` (falling back to
``pages/<image_ref>.png``). Pages without a PNG are skipped with a stderr
warning -- never fatal, so sparse checkouts degrade to fewer items.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-root", required=True, help="study directory")
    parser.add_argument("--output", required=True, help="items JSON output")
    parser.add_argument("--max-items", type=int, default=0)
    args = parser.parse_args()

    study = Path(args.study_root)
    try:
        pages = json.loads((study / "ocr_pages.json").read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"make_study_items: cannot read ocr_pages.json: {exc}",
              file=sys.stderr)
        return 1
    if not isinstance(pages, list):
        print("make_study_items: ocr_pages.json must hold a list",
              file=sys.stderr)
        return 1

    items = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_id = page.get("page_id", "")
        if not page_id:
            continue
        candidates = [study / "pages" / f"{page_id}.png"]
        image_ref = page.get("image_ref", "")
        if image_ref and image_ref != page_id:
            candidates.append(study / "pages" / f"{image_ref}.png")
        image = next((c for c in candidates if c.is_file()), None)
        if image is None:
            print(f"make_study_items: skipping {page_id}: no PNG",
                  file=sys.stderr)
            continue
        items.append({"id": page_id, "image": str(image.resolve())})
    if args.max_items > 0:
        items = items[: args.max_items]
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"study items={len(items)} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
