#!/usr/bin/env python3
"""DeepSeek-OCR v2 adapter: images -> hypotheses JSONL (common schema).

Uses the vendor transformers recipe (AutoModel, trust_remote_code) against a
local weights dir. Page prompt, crop prompt, vision sizes, dtype and weight
quantisation are config knobs and are recorded verbatim in the
{output}.meta.json sidecar along with peak VRAM, so the report provenance
captures the full setup.

Findings (bio10, RTX 2060 Super 8 GB, torch 2.6 cu124, 2026-09-11):
- The v2 vision encoder only has learned query grids for 768 px crops
  (144 queries) and 1024 px global views (256 queries); any other
  ``image_size``/``base_size`` raises UnboundLocalError('param_img').
- The vendor ``infer`` casts images to bfloat16 and runs under
  ``autocast(bfloat16)``; loading the model in float16 fails with a
  masked_scatter_ dtype mismatch, so the default dtype is bfloat16.
- Full-precision weights (6.4 GB) leave too little of the 8 GB card for
  activations: every item OOMs even without crop mode. ``--quantize 8bit``
  (bitsandbytes LLM.int8 on the language model only; vision encoder,
  projector and lm_head stay in bfloat16) is the configuration that fits.
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import add_common_args, load_items, selected, summarize, write_rows  # noqa: E402

# Kept un-quantised: the SAM/Qwen2 vision encoder, the projector and the
# output head (bitsandbytes matches these as module-name substrings).
_SKIP_QUANT = ["sam_model", "qwen2_model", "projector", "view_seperator", "lm_head"]


def main() -> None:
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    parser.add_argument("--weights-dir", required=True, help="local DeepSeek-OCR-2 dir")
    parser.add_argument("--prompt-page", default="Free OCR.")
    parser.add_argument("--prompt-crop", default="Free OCR.")
    parser.add_argument("--base-size", type=int, default=1024, choices=[1024])
    parser.add_argument("--image-size", type=int, default=768, choices=[768, 1024])
    parser.add_argument("--no-crop-mode", action="store_true")
    parser.add_argument("--dtype", default="bfloat16", choices=["bfloat16", "float16"])
    parser.add_argument("--quantize", default="none", choices=["none", "8bit", "4bit"])
    args = parser.parse_args()

    import torch
    from transformers import AutoModel, AutoTokenizer

    dtype = getattr(torch, args.dtype)
    weights = str(Path(args.weights_dir).resolve())
    tokenizer = AutoTokenizer.from_pretrained(weights, trust_remote_code=True)
    load_kwargs: dict = {"trust_remote_code": True, "torch_dtype": dtype}
    if args.quantize != "none":
        from transformers import BitsAndBytesConfig

        load_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_8bit=args.quantize == "8bit",
            load_in_4bit=args.quantize == "4bit",
            bnb_4bit_compute_dtype=dtype,
            llm_int8_skip_modules=_SKIP_QUANT,
        )
        load_kwargs["device_map"] = {"": 0}
    model = AutoModel.from_pretrained(weights, **load_kwargs).eval()
    if torch.cuda.is_available():
        if args.quantize == "none":
            model = model.cuda()
        torch.cuda.reset_peak_memory_stats()
    rev = (
        args.model_rev
        if args.model_rev != "unknown"
        else f"deepseek-ocr-2-local-{args.dtype}-q{args.quantize}-torch{torch.__version__}"
    )

    items = selected(load_items(args.items), args.max_items)
    out_dir = Path(args.output).parent / "dsocr2_raw"
    out_dir.mkdir(parents=True, exist_ok=True)

    def infer(image: str) -> str:
        prompt = args.prompt_crop if args.split == "mozhi-test" else args.prompt_page
        with torch.inference_mode():
            text = model.infer(
                tokenizer,
                prompt=f"<image>\n{prompt}",
                image_file=image,
                output_path=str(out_dir),
                base_size=args.base_size,
                image_size=args.image_size,
                crop_mode=not args.no_crop_mode,
                save_results=False,
                eval_mode=True,
            )
        if text is None:
            raise RuntimeError("infer returned no text")
        return text

    def cleanup() -> None:
        # One OOM must not poison every later item: release cached blocks.
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        for item in items:
            future = pool.submit(_guarded, infer, cleanup, item.get("image", ""))
            try:
                text, error, elapsed = future.result(timeout=args.timeout)
            except FuturesTimeoutError:
                text, error, elapsed = "", "timeout", args.timeout
            rows.append(
                {"id": item["id"], "text": text, "seconds": round(elapsed, 3), "error": error, "model_rev": rev}
            )
    write_rows(rows, args.output)

    meta = {
        "engine": "deepseekocr",
        "split": args.split,
        "model_rev": rev,
        "weights_dir": weights,
        "prompt_page": args.prompt_page,
        "prompt_crop": args.prompt_crop,
        "base_size": args.base_size,
        "image_size": args.image_size,
        "crop_mode": not args.no_crop_mode,
        "dtype": args.dtype,
        "quantize": args.quantize,
        "torch": torch.__version__,
        "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 1e9, 2)
        if torch.cuda.is_available()
        else None,
    }
    Path(str(args.output) + ".meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"deepseekocr split={args.split} {summarize(rows)} peak_vram={meta['peak_vram_gb']}GB")


def _guarded(infer, cleanup, image: str):
    start = time.monotonic()
    try:
        if not image or not Path(image).is_file():
            return "", f"missing-file:{image}", time.monotonic() - start
        return infer(image) or "", None, time.monotonic() - start
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}:{str(exc)[:200]}", time.monotonic() - start
    finally:
        cleanup()


if __name__ == "__main__":
    main()
