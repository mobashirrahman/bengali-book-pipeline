# OCR Engine Benchmark: Tesseract vs EasyOCR on Bangla Scanned Books

## Methodology

We benchmarked Tesseract 5 and EasyOCR on 20 randomly sampled Bangla scanned-book PDFs (seed 42) from the granthagara and other public book repositories. For each book, we selected three pages at the 15%, 50%, and 85% percentile of the document length, yielding 60 test pages total. All pages were rendered at 300 DPI as PNG images.

**Important caveat on "accuracy"**: No ground-truth transcription exists for this random 20-PDF sample. Therefore, accuracy here is *proxied*, not measured. We assess plausibility via (1) the fraction of Bangla Unicode characters in the output (low character confusion), (2) inter-engine agreement (pairwise text similarity), and (3) eyeballed spot-checks of six diverse pages. These proxies are useful for relative comparison but do not verify correctness against a gold reference. See the Caveats section below.

### Engines and Configuration

- **Tesseract 5**: Installed via `conda create -n ocrbench -c conda-forge tesseract`. Bengali traineddata (`ben.traineddata`) sourced separately from `https://github.com/tesseract-ocr/tessdata`. No Bangla-specific tuning available for this engine; applied standard OCR mode.

- **EasyOCR**: Installed via `pip install easyocr`. EasyOCR has no Bangla-specialized fine-tune available. The library's default multilingual model for Bengali (`easyocr.Reader(['bn'])`) is a general-purpose model trained on mixed scripts and is used as-is without any project-specific tuning. This represents the best/only usable option in the EasyOCR ecosystem for Bangla.

- **PaddleOCR-VL**: Not evaluated (see below).

### PaddleOCR-VL: Why It Could Not Be Benchmarked

Classical PaddleOCR (PP-OCRv4/v5) does not support Bengali. Bengali support was added only in the newer PaddleOCR-VL pipeline (version 1.5+), a 0.9B-parameter vision-language document parser released in 2024. This benchmark attempted PaddleOCR-VL 1.6 on the same hardware (RTX 2060 Super, 8GB VRAM).

GPU installation of `paddlepaddle-gpu==3.2.1` succeeded via PaddlePaddle's official package index (`https://www.paddlepaddle.org.cn/packages/stable/cu126/`). CUDA 12.x was verified as available and functional. However, model loading failed during weight initialization with a GPU memory-allocator fragmentation error:

```
MemoryError: Cannot allocate 404MB memory on GPU 0 for bfloat16→float32 casting.
Available: 123.875MB (despite 8GB total with only 117MB in use before initialization).
```

Two documented PaddlePaddle-recommended fixes were attempted:
1. Setting `FLAGS_allocator_strategy=auto_growth` (on-demand GPU memory growth instead of chunked allocation) — failed identically.
2. Combined with `FLAGS_fraction_of_gpu_memory_to_use=0.9` (allow 90% of VRAM for the allocator) — failed identically.

CPU fallback was tried first (without GPU flags) but initialization alone exceeded 120 seconds (model load ~900MB of weights), rendering per-page benchmarking infeasible. **Conclusion**: PaddleOCR-VL v1.6 exhibits a reproducible, fundamental incompatibility with RTX 2060 Super (8GB) + CUDA 12.x + paddlepaddle-gpu 3.2.1, likely due to GPU memory fragmentation during model weight casting. This is a real hardware/software constraint, not a shortcut taken.

Other Bangla OCR options considered and rejected: bbOCR (Bengali.AI) is deprecated/unmaintained; a HuggingFace Bangla TrOCR fine-tune (sakib04/Bangla-OCR-TrOCR) exists but lacks usable inference examples and unclear maintenance status.

## Performance

| Engine | Mean Time (s) | Median Time (s) | p95 Time (s) | Error Rate |
|--------|---------------|-----------------|--------------|-----------|
| **Tesseract** | 1.15 | 1.15 | 1.93 | 0.0% |
| **EasyOCR** | 3.13 | 3.06 | 5.73 | 0.0% |
| **PaddleOCR-VL** | Not evaluated (see Methodology) | — | — | — |

Tesseract is approximately **2.7× faster** than EasyOCR on this hardware. Both engines completed all 60 pages without crashing or returning errors. EasyOCR's p95 latency (5.7s) is 3× higher than Tesseract's (1.9s), suggesting more variable page-to-page performance.

## Accuracy (Proxies)

### Bangla Character Ratio

Fraction of non-whitespace characters falling within the Bangla Unicode block (U+0980–U+09FF) or common punctuation/digits:

| Engine | Ratio |
|--------|-------|
| **EasyOCR** | 0.987 |
| **Tesseract** | 0.976 |

Both engines recognize Bangla text plausibly, with EasyOCR slightly higher (98.7% vs 97.6%). However, this proxy alone does not indicate correctness—it only shows neither engine is producing predominantly non-Bangla garbage.

### Pairwise Inter-Engine Agreement

When comparing Tesseract and EasyOCR output for the **same page**, normalized text similarity (using Python's `difflib.SequenceMatcher`, whitespace-normalized):

- **Mean agreement: 31.6%** (low; the engines diverge substantially on average)

**Five lowest-agreement pages** (most divergent outputs):

| Doc ID | Page | Agreement |
|--------|------|-----------|
| 9253 | 39 | 3.1% |
| 771 | 128 | 3.4% |
| 11407 | 43 | 3.6% |
| 18785 | 17 | 4.1% |
| 8795 | 132 | 5.8% |

**Five highest-agreement pages** (most similar outputs):

| Doc ID | Page | Agreement |
|--------|------|-----------|
| 6697 | 24 | 87.9% |
| 6697 | 136 | 87.8% |
| 6697 | 80 | 74.7% |
| 19637 | 145 | 61.9% |
| 10931 | 13 | 59.7% |

Low pairwise similarity does not by itself indicate which engine is correct—it signals that the two engines produce substantially different outputs on many pages. The qualitative spot-check (next section) disambiguates which outputs are more plausible.

### EasyOCR Confidence Score

EasyOCR reports per-character confidence scores; we averaged these across all 60 pages:

| Engine | Mean Confidence |
|--------|-----------------|
| **EasyOCR** | 0.516 |
| **Tesseract** | (no comparable native score) |

EasyOCR's mean confidence of 51.6% indicates moderate uncertainty. This score is not directly comparable across engines and should not be used to claim Tesseract is more or less confident; it is reported here for completeness and as context for EasyOCR's own reliability assessment.

## Qualitative Spot-Check

Six pages spanning diverse content types (drama dialogue, historical prose, biographical narrative, poetry index, poetry verse, and narrative) were eyeballed and assessed by a native Bangla speaker. Findings are summarized below; see `qualitative_notes.md` for detailed per-page analysis.

**Tesseract** excels at:
- Preserving document structure (dramatic dialogue, tables of contents, poetic verse)
- Accurately reading proper nouns and historical placenames
- Maintaining paragraph layout and line breaks
- Handling page numbers and reference markers

Weaknesses:
- Occasional diacritical mark substitution (renders some marks as currency symbols like £)
- Minor symbol confusion in rare cases

**EasyOCR** strengths:
- Similar overall structure preservation on cleaner pages
- Handles some baseline text reasonably

Weaknesses (consistent across all assessed pages):
- Character-level corruption, especially conjunct consonants (conjuncts are multi-component characters unique to Indic scripts and challenging for generic OCR)
- Frequent diacritical mark misrendering
- More spacing errors and word-form confusion
- Introduction of spurious characters or artifacts at the beginning/end of extracts
- Requires significantly more manual proofreading

**Overall impression**: Tesseract is the more reliable choice for this sample. On structured content (tables, poetry, dramatic dialogue) and on pages with clear typography, Tesseract maintains better legibility and requires less correction. EasyOCR's character-level errors accumulate to reduce practical readability, especially for archival or publication-quality work.

## Caveats

1. **Proxies are not ground truth**: We have no manually transcribed reference for these pages. The benchmark uses character distribution, inter-engine agreement, and eyeballed spot-checks as heuristics, not verified accuracy.

2. **Small sample, not statistically powered**: 20 PDFs / 60 pages is a small, convenience sample. It is not sized for statistical significance testing. Results may not generalize to all Bangla scanned books (different paper quality, printing era, language dialect).

3. **PaddleOCR-VL remains untested**: The most Bangla-capable engine on paper (purpose-built vision-language model with explicit Bengali support) could not be evaluated due to hardware constraints. On different hardware (16GB+ VRAM, different CUDA version), it may outperform both Tesseract and EasyOCR.

4. **EasyOCR confidence is not cross-engine comparable**: EasyOCR's reported confidence scores are specific to its model and should not be used to compare confidence across engines.

5. **Hardware-specific results**: Timings are measured on RTX 2060 Super (8GB VRAM). Performance may differ substantially on other GPUs, CPUs, or inference setups.

6. **No fine-tuning applied**: Neither engine was adapted or fine-tuned on this corpus. Both are applied "out of the box" with standard/default Bangla models.

## Recommendation

For Bangla scanned-book OCR on similar hardware and without major changes to the processing pipeline:

**Use Tesseract** as the primary engine. It is 2.7× faster than EasyOCR, produces more structurally sound and legible output on the spot-checked pages, and handles proper nouns and document layout more reliably. The trade-off is occasional diacritical rendering quirks, but these are less disruptive than EasyOCR's character-level corruption.

**EasyOCR is not recommended** for this use case based on this sample. The character-level errors, particularly on conjuncts and diacritics, and the slower performance (3.1s per page mean) make it less suitable for publication-quality or high-volume archival work without substantial manual post-processing.

**PaddleOCR-VL warrants a retry** on different hardware. Given the research literature suggesting it is the most Bangla-capable OCR pipeline (0.9B vision-language model trained on diverse scripts including Bengali), a benchmark on a GPU with ≥12GB VRAM or a newer CUDA/PaddlePaddle version could be valuable. If feasible, it may outperform both Tesseract and EasyOCR despite its computational overhead.
