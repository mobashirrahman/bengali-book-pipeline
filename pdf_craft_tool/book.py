"""Restartable local Bengali book pipeline; no credentials or model downloads."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time
from urllib.request import Request, urlopen

from pypdf import PdfReader

from pdf_craft import PDFCraft, PDFCraftExtraction, PDFOptions, ExtractionOptions, TesseractOCRLocalConfig
from pdf_craft.pdf.tesseract import TesseractPageExtractor
from pdf_craft.pdf.crop_verifier import TesseractCropVerifier
from pdf_craft.renderer.markdown.bundle import render_markdown_bundle
from pdf_craft.transformer.proofreader import ConservativeProofreader, PROOFREAD_PROMPT


def add_book_command(commands) -> None:
    parser = commands.add_parser("book", help="Tesseract -> audited proofreading -> EPUB + Markdown/chunks")
    parser.add_argument("source", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--pages", help="1-based inclusive ranges, e.g. 4-13,100,200-205")
    parser.add_argument("--stage", choices=("all", "extract", "proofread", "render"), default="all")
    parser.add_argument("--tesseract", default="tesseract")
    parser.add_argument("--tessdata", type=Path, required=True, help="directory containing tessdata_best/ben.traineddata")
    parser.add_argument("--easyocr-fallback", action="store_true", help="retain an alternate reading of weak pages")
    parser.add_argument("--easyocr-model-path", type=Path)
    parser.add_argument("--no-proofread", action="store_true")
    parser.add_argument("--review-only", action="store_true", help="audit proposals without applying them")
    parser.add_argument("--proofread-all", action="store_true", help="also inspect high-confidence text blocks")
    parser.add_argument("--protected-word", action="append", default=[])
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3.5:4b")
    parser.add_argument("--vision-proofread", action="store_true", help="send paragraph image crops to a vision-capable model")
    parser.add_argument("--chunk-tokens", type=int, default=800)
    parser.add_argument("--title")
    parser.add_argument("--author", action="append", default=[])
    parser.add_argument("--metadata", type=Path, help="metadata JSON; defaults to SOURCE.metadata.json")
    parser.add_argument("--epubcheck", type=Path, help="optional EPUBCheck JAR; validation errors fail the run")
    parser.set_defaults(handler=run_book)


def parse_pages(value: str | None, count: int) -> list[int]:
    if value is None:
        return list(range(1, count + 1))
    pages = set()
    for part in value.split(","):
        pair = part.split("-")
        if len(pair) > 2 or not all(item.strip().isdigit() for item in pair):
            raise ValueError("Pages must be comma-separated positive integers or ranges")
        first, last = int(pair[0]), int(pair[-1])
        if not 1 <= first <= last <= count:
            raise ValueError(f"Page range {part!r} is outside 1-{count}")
        pages.update(range(first, last + 1))
    return sorted(pages)


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def verify_artifact(path: Path) -> None:
    """Detect changed/truncated cached artifacts instead of silently reusing them."""
    seal = path.with_suffix(path.suffix + ".sha256")
    actual = file_hash(path)
    if seal.exists() and seal.read_text(encoding="utf-8") != actual:
        raise ValueError(f"Cached artifact changed: {path}; use a fresh work directory")
    if not seal.exists():
        seal.write_text(actual, encoding="utf-8")


def _stage(root: Path, kind: str, settings: dict) -> Path:
    path = root / kind / fingerprint(settings)[:20]
    path.mkdir(parents=True, exist_ok=True)
    manifest = path / "settings.json"
    if manifest.exists():
        if fingerprint(json.loads(manifest.read_text(encoding="utf-8"))) != fingerprint(settings):
            raise ValueError(f"Stage identity collision at {path}")
    elif any(path.iterdir()):
        raise ValueError(f"Refusing to reuse an unowned stage directory: {path}")
    else:
        write_json(manifest, settings)
    return path


class OllamaProofreadingClient:
    """Use Ollama's local JSON chat API with thinking disabled and bounded output."""

    def __init__(self, url: str, model: str) -> None:
        self.url, self.model = url.rstrip("/"), model
        with urlopen(self.url + "/api/tags", timeout=15) as response:
            models = json.load(response)["models"]
        record = next((item for item in models if item["name"] == model), None)
        if record is None:
            raise ValueError(f"Model {model!r} is not installed in Ollama; install it explicitly first")
        self.identity = f"{model}@{record['digest']}"

    def __call__(self, system: str, user: str) -> str:
        return self.chat(system, user)

    def chat(self, system: str, user: str, images: list[str] | None = None) -> str:
        payload = {"model": self.model, "stream": False, "think": False, "format": "json",
                   "keep_alive": "5m", "options": {"temperature": 0, "seed": 0,
                   "num_ctx": 4096, "num_predict": 512}, "messages": [
                       {"role": "system", "content": system}, {"role": "user", "content": user}]}
        if images:
            payload["messages"][1]["images"] = images
        request = Request(self.url + "/api/chat", json.dumps(payload).encode(), {"Content-Type": "application/json"})
        with urlopen(request, timeout=180) as response:
            result = json.load(response)
        if not result.get("done") or result.get("done_reason") == "length":
            raise RuntimeError("Incomplete LLM response; original text retained")
        return result["message"]["content"]


def run_book(args: argparse.Namespace) -> int:
    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".book.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another book pipeline is using this work directory") from error
        return _run_book(args, root)


def _run_book(args, root: Path) -> int:
    started = time.monotonic()
    source = args.source.resolve()
    source_id = file_hash(source)
    pages = parse_pages(args.pages, len(PdfReader(source).pages))
    config = TesseractOCRLocalConfig(executable=args.tesseract, tessdata_path=args.tessdata,
                                    easyocr_fallback=args.easyocr_fallback,
                                    easyocr_model_path=args.easyocr_model_path)
    # Preflight prevents a missing executable/model from producing an image-only book.
    adapter = TesseractPageExtractor(config)
    adapter.load_ocr_model()
    executable = shutil.which(config.executable)
    if executable is None:
        raise FileNotFoundError(config.executable)
    ocr_settings = {"protocol": "bengali-ocr-1", "source": source_id, "pages": pages,
                    "dpi": 300, "config": asdict(config), "binary": file_hash(Path(executable)),
                    "language_data": file_hash(args.tessdata / "ben.traineddata")}
    ocr_root = _stage(root, "ocr", ocr_settings)
    raw = ocr_root / "raw.pcex"
    craft = PDFCraft(PDFOptions(ocr=config))
    if not raw.exists():
        if args.stage in ("proofread", "render"):
            raise FileNotFoundError("Run --stage extract with the same OCR settings first")
        print(f"OCR: {len(pages)} pages -> {raw}", flush=True)
        def progress(event):
            if event.kind.name in ("COMPLETE", "FAILED"):
                print(f"  page {event.page_index}: {event.kind.name.lower()} ({event.cost_time_ms} ms)", flush=True)
        craft.extract_pdf(source, raw, ExtractionOptions(
            page_indexes=set(pages), dpi=300, includes_cover=1 in pages,
            ignore_ocr_errors=True, on_ocr_event=progress), analysing_path=ocr_root / "analysis")
    extraction = PDFCraftExtraction.open(raw)
    verify_artifact(raw)
    craft.release_pdf_resources()
    diagnostics_path = ocr_root / "analysis" / "ocr"
    reviews = []
    for page in pages:
        path = diagnostics_path / f"page_{page}.json"
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("needs_review"):
                reviews.append({"page": page, "reason": record.get("reason"), "diagnostics": str(path)})
    summary = {"source": str(source), "source_sha256": source_id, "pages": pages,
               "raw": str(raw), "ocr_review": reviews, "proofreading": "not run"}
    if args.stage != "extract" and not args.no_proofread:
        client = OllamaProofreadingClient(args.ollama_url, args.model)
        if getattr(args, "vision_proofread", False):
            from .vision import VisionProofreadingClient
            client = VisionProofreadingClient(client, source, extraction, source_id, file_hash(raw))
        proof_settings = {"protocol": "span-proofreader-2-crop-evidence", "raw": file_hash(raw),
                          "model": client.identity, "endpoint": args.ollama_url,
                          "prompt": PROOFREAD_PROMPT, "apply": not args.review_only,
                          "all_blocks": args.proofread_all, "protected": args.protected_word}
        proof_root = _stage(root, "proofread", proof_settings)
        proof_path = proof_root / "proofread.pcex"
        if not proof_path.exists():
            if args.stage == "render":
                raise FileNotFoundError("Run --stage proofread with these settings first")
            print(f"Proofreading: {client.identity} -> {proof_path}", flush=True)
            diagnostics = {page: json.loads(path.read_text(encoding="utf-8")) for page in pages
                           if (path := diagnostics_path / f"page_{page}.json").exists()}
            verifier = TesseractCropVerifier(source, config, diagnostics, proof_root / "evidence")
            try:
                proofreader = ConservativeProofreader(
                    client, model_identity=client.identity, audit_path=proof_root / "audit",
                    diagnostics=None if args.proofread_all else diagnostics,
                    verify_edit=verifier,
                    apply_edits=not args.review_only, protected_words=tuple(args.protected_word))
                craft.translate_extraction(extraction, proof_path, proofreader)
            finally:
                verifier.close()
                if hasattr(client, "close"):
                    client.close()
        extraction = PDFCraftExtraction.open(proof_path)
        verify_artifact(proof_path)
        summary.update(proofreading=str(proof_path), audit=str(proof_root / "audit"))
        audit = [json.loads(path.read_text(encoding="utf-8")) for path in (proof_root / "audit").glob("p*.json")]
        summary["proofreading_counts"] = {
            "inspected_blocks": len(audit), "changed_blocks": sum(row["status"] == "changed" for row in audit),
            "review_blocks": sum(row["status"] == "review" or any(item["status"] == "review" for item in row.get("decisions", [])) for row in audit)}
    if args.stage in ("all", "render"):
        from .book_metadata import load_metadata, publication_settings, prepare_publication
        metadata = load_metadata(source, metadata_path=getattr(args, "metadata", None),
                                 title=args.title, authors=args.author)
        render_settings = {"protocol": "book-bundle-4-reading-structure", "extraction": file_hash(
            Path(summary["proofreading"]) if summary["proofreading"] != "not run" else raw),
            "chunk_tokens": args.chunk_tokens, "metadata": publication_settings(metadata)}
        render_root = _stage(root, "render", render_settings)
        output = render_root / "book"
        if not output.exists():
            with TemporaryDirectory(dir=render_root, prefix="render-") as temporary:
                bundle = Path(temporary) / "book"
                bundle.mkdir()
                from pdf_craft.transformer.reading_structure import recover_structure
                extraction = recover_structure(extraction, bundle / "reading.pcex", bundle / "structure-audit.json",
                                               metadata.get("structure", []))
                render_markdown_bundle(extraction, bundle, source_id=source_id, chunk_tokens=args.chunk_tokens)
                book_meta, publication = prepare_publication(metadata, source, source_id, bundle)
                craft.render_epub(extraction, bundle / "book.epub", lan="bn",
                                  book_meta=book_meta, publication=publication)
                from pdf_craft.renderer.epub.validation import validate_publication
                write_json(bundle / "validation.json", validate_publication(bundle / "book.epub"))
                bundle.rename(output)
        for artifact in (output / "book.epub", output / "book.md", output / "chunks.jsonl", output / "source-map.json"):
            verify_artifact(artifact)
        if args.epubcheck:
            result = subprocess.run(["java", "-jar", str(args.epubcheck.resolve()), str(output / "book.epub")],
                                    capture_output=True, text=True, timeout=120, check=False)
            (render_root / "epubcheck.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
            if result.returncode:
                raise RuntimeError(f"EPUBCheck failed; see {render_root / 'epubcheck.txt'}")
            summary["epubcheck"] = "passed"
        else:
            summary["epubcheck"] = "not run"
        summary["output"] = str(output)
    summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
    write_json(root / "run-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0
