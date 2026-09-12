"""Re-publish saved extraction without OCR, GPU, or LLM calls."""

from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess

from pdf_craft import PDFCraftExtraction, EpubRenderer
from pdf_craft.renderer.markdown.bundle import render_markdown_bundle
from pdf_craft.renderer.epub.validation import validate_publication
from .book import file_hash, fingerprint, write_json
from .book_metadata import load_metadata, publication_settings, prepare_publication


def add_publish_command(commands):
    parser = commands.add_parser("publish", help="saved .pcex -> enriched EPUB/Markdown, no OCR or LLM")
    parser.add_argument("extraction", type=Path)
    parser.add_argument("--source", type=Path, required=True, help="original PDF for source identity and optional cover page")
    parser.add_argument("--output-dir", type=Path, required=True, help="new directory; existing outputs are never overwritten")
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--chunk-tokens", type=int, default=800)
    parser.add_argument("--epubcheck", type=Path, help="EPUBCheck JAR; fail before publishing if conformance checks fail")
    parser.set_defaults(handler=publish)


def publish(args):
    output = args.output_dir.resolve()
    if output.exists():
        raise FileExistsError(f"Use a new output directory: {output}")
    source = args.source.resolve()
    extraction = PDFCraftExtraction.open(args.extraction)
    values = load_metadata(source, metadata_path=args.metadata)
    source_id = file_hash(source)
    settings = {"protocol": "publication-3-reading-structure", "extraction_sha256": file_hash(args.extraction),
                "source_sha256": source_id, "metadata": publication_settings(values), "chunk_tokens": args.chunk_tokens}
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent, prefix="publish-") as temporary:
        bundle = Path(temporary) / "book"
        bundle.mkdir()
        from pdf_craft.transformer.reading_structure import recover_structure
        extraction = recover_structure(extraction, bundle / "reading.pcex", bundle / "structure-audit.json",
                                       values.get("structure", []))
        render_markdown_bundle(extraction, bundle, source_id=source_id, chunk_tokens=args.chunk_tokens)
        meta, options = prepare_publication(values, source, source_id, bundle)
        EpubRenderer().render(extraction, bundle / "book.epub", lan=extraction.language() or "bn",
                              book_meta=meta, publication=options)
        write_json(bundle / "validation.json", validate_publication(bundle / "book.epub"))
        if args.epubcheck:
            result = subprocess.run(["java", "-jar", str(args.epubcheck.resolve()), str(bundle / "book.epub")],
                                    capture_output=True, text=True, timeout=120, check=False)
            if result.returncode:
                raise RuntimeError("EPUBCheck failed: " + result.stdout + result.stderr)
            (bundle / "epubcheck.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
        write_json(bundle / "publication-settings.json", {**settings, "fingerprint": fingerprint(settings)})
        for artifact in ("book.epub", "book.md", "chunks.jsonl", "source-map.json", "metadata.json", "reading.pcex", "structure-audit.json"):
            (bundle / (artifact + ".sha256")).write_text(file_hash(bundle / artifact), encoding="utf-8")
        bundle.rename(output)
    print(output)
    return 0
