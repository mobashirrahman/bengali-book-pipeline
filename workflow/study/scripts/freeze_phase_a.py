"""Freeze the Phase-A study root from the untouched pilot study (read-only).

Copies the pilot's JSON artefacts (manifest, annotation seed, B0 pages,
provenance) into the new study root and symlinks (never duplicates) the
rendered page PNGs. Writes STUDY.json provenance incl. the pilot manifest
sha256 so the two datasets stay linked by input hashes, never by judgments.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import shutil
from pathlib import Path

COPY = ("sample_manifest.json", "annotation_pages.json", "ocr_pages.json",
        "pilot_provenance.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", required=True, help="pilot study root (read-only)")
    parser.add_argument("--study", required=True, help="new study root to create")
    parser.add_argument("--study-id", required=True)
    parser.add_argument("--phase", default="phase-a-60")
    parser.add_argument("--out", required=True, help="STUDY.json output path")
    args = parser.parse_args()

    pilot = Path(args.pilot)
    study = Path(args.study)
    if not pilot.is_dir():
        raise SystemExit(f"pilot study root missing: {pilot}")
    study.mkdir(parents=True, exist_ok=True)

    manifest_src = pilot / "sample_manifest.json"
    if not manifest_src.is_file():
        raise SystemExit(f"pilot manifest missing: {manifest_src}")
    for name in COPY:
        src = pilot / name
        if not src.is_file():
            raise SystemExit(f"pilot artefact missing: {src}")
        shutil.copyfile(src, study / name)

    pages_src = pilot / "pages"
    pages_dst = study / "pages"
    if pages_dst.is_symlink() or pages_dst.exists():
        if not (pages_dst.is_symlink() and pages_dst.resolve() == pages_src.resolve()):
            raise SystemExit(f"refusing to overwrite {pages_dst}")
    elif pages_src.is_dir():
        pages_dst.symlink_to(pages_src.resolve(), target_is_directory=True)
    else:
        raise SystemExit(f"pilot pages dir missing: {pages_src}")

    manifest_hash = _sha256(manifest_src)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "study_id": args.study_id,
        "phase": args.phase,
        "pilot_study": str(pilot),
        "pilot_manifest_sha256": manifest_hash,
        "frozen_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "note": "inputs shared by hash with the pilot; no pilot judgments reused",
    }, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"froze {args.phase} from {pilot} -> {study} "
          f"(manifest {manifest_hash[:16]}...)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
