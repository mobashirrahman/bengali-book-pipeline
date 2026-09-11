"""Verify pinned benchmark inputs (hashes + counts) -> inputs_meta.json."""

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reid-zip", required=True)
    parser.add_argument("--mozhi-test-zip", required=True)
    parser.add_argument("--mozhi-val-zip", required=True)
    parser.add_argument("--reid-dir", required=True)
    parser.add_argument("--mozhi-test-dir", required=True)
    parser.add_argument("--mozhi-val-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    zips = {
        "reid_zip": Path(args.reid_zip),
        "mozhi_test_zip": Path(args.mozhi_test_zip),
        "mozhi_val_zip": Path(args.mozhi_val_zip),
    }
    for name, path in zips.items():
        assert path.is_file(), f"missing pinned input: {path}"

    reid_dir = Path(args.reid_dir)
    mozhi_test_dir = Path(args.mozhi_test_dir)
    mozhi_val_dir = Path(args.mozhi_val_dir)
    meta = {
        "zips": {name: {"path": str(p), "sha256": sha256(p)} for name, p in zips.items()},
        "reid": {
            "dir": str(reid_dir),
            "xml": sum(1 for _ in reid_dir.glob("*.xml")),
            "tif": sum(1 for _ in reid_dir.glob("*.tif*")),
        },
        "mozhi_test": {
            "dir": str(mozhi_test_dir),
            "gt_lines": sum(1 for _ in (mozhi_test_dir / "test_gt.txt").open(encoding="utf-8")),
            "images": sum(1 for _ in (mozhi_test_dir / "images").glob("*")),
        },
        "mozhi_val": {
            "dir": str(mozhi_val_dir),
            "gt_lines": sum(1 for _ in (mozhi_val_dir / "val_gt.txt").open(encoding="utf-8")),
            "images": sum(1 for _ in (mozhi_val_dir / "images").glob("*")),
        },
    }
    assert meta["reid"]["xml"] > 0 and meta["reid"]["tif"] > 0, "REID dir looks empty"
    assert meta["mozhi_test"]["images"] > 0, "Mozhi test images missing"

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
