#!/usr/bin/env python3
"""EasyOCR adapter (bn): images -> hypotheses JSONL (common schema)."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import add_common_args, load_items, run_sequential, selected, summarize, write_rows  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    parser.add_argument("--recog-network", default=None,
                        help="custom recognition network (e.g. bengali_crnn)")
    parser.add_argument("--user-network-directory", default=None)
    args = parser.parse_args()

    import easyocr

    # A custom recognizer's weights live alongside its network definition
    # (config.yaml: crnn_dir holds bengali_crnn.pth/.py/.yaml and serves as
    # both model_storage_directory and user_network_directory) -- EasyOCR's
    # get_recognizer() loads the .pth from model_storage_directory, so the
    # stock EASYOCR_MODEL_DIR (which only has the default bengali.pth) must
    # not shadow it when a custom recog network is requested.
    model_storage_directory = args.user_network_directory or os.environ.get("EASYOCR_MODEL_DIR") or None
    reader_kwargs: dict = {
        "lang_list": ["bn"],
        "gpu": (args.device == "cuda"),
        "model_storage_directory": model_storage_directory,
    }
    if args.recog_network:
        reader_kwargs["recog_network"] = args.recog_network
    if args.user_network_directory:
        reader_kwargs["user_network_directory"] = args.user_network_directory
    reader = easyocr.Reader(**{k: v for k, v in reader_kwargs.items() if v is not None})
    net = args.recog_network or "default"
    rev = args.model_rev if args.model_rev != "unknown" else f"easyocr-{easyocr.__version__}-bn-{args.device}-{net}"

    def infer(image: str) -> str:
        texts = reader.readtext(image, detail=0)
        return "\n".join(texts)

    items = selected(load_items(args.items), args.max_items)
    rows = run_sequential(items, infer, args.timeout, rev)
    write_rows(rows, args.output)
    print(f"easyocr split={args.split} {summarize(rows)}")


if __name__ == "__main__":
    main()
