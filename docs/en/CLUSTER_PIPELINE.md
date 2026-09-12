# Continuous book processing on bio01–bio24

The farm watches `data/` on bio10, transfers stable PDFs to available GPU servers,
and brings back EPUB, Markdown, chapter files, JSONL chunks and audit records.
Each server processes one book at a time. All 24 hostnames remain in the pool;
offline hosts and busy/broken GPUs are retried automatically.

The current production model is `qwen3.5:4b`, with Tesseract Bengali OCR and
source-image checks before applying corrections. The fleet does not split one
model across GPUs and does not run two Qwen models on every passage.

## Daily operation

Add PDFs anywhere below `/scratch/pdf-craft/data`. Subdirectories and Bengali
filenames are supported. The watcher scans every minute and waits until a file
has stayed unchanged for two minutes. There is no manual per-book submission.
Identical PDFs at different paths share one job. A modified PDF receives a new
content identity; previous results remain available.

On bio10:

```bash
systemctl --user status pdf-craft-cluster.service

.venv/bin/python -m pdf_craft_tool.cluster status \
  --config pdf-craft-output/cluster-config.json

tail -f pdf-craft-output/cluster/coordinator.log
```

The service is enabled and user lingering keeps it alive after logout. It restarts
after a coordinator failure. Its configuration and all processing output live under
`pdf-craft-output/`, outside the watched input tree.

`status` reports counts for pending, running, retry, failed and done jobs, plus host
health. A ready worker may be waiting for input. An unreachable worker may still
have a running job; ownership is retained until reconnection establishes its result.

## Outputs and review

Find each completed book in:

```text
pdf-craft-output/cluster/jobs/<job-id>/
  job.json
  summary.json             # source/processing details and local output paths
  result.json              # worker result, hostname assignment in queue
  worker.log
  ollama.log
  work/
    ocr/<fingerprint>/raw.pcex
    proofread/<fingerprint>/proofread.pcex
    proofread/<fingerprint>/audit/
    proofread/<fingerprint>/evidence/
    render/<fingerprint>/book/
      book.epub
      book.md
      chapters/
      assets/
      chunks.jsonl
      source-map.json
```

Use `summary.json` for current paths instead of assuming a stage fingerprint.
Results become `done` only after the four reader artifacts have been copied back
and their SHA-256 checksums verified. `done` means conversion completed; a book can
still have OCR quality warnings or proofreading proposals needing review.

The original PDF stays in `data/`. A worker verifies the input hash before OCR.
Source page numbers are physical 1-based PDF pages, and source-map boxes use OCR
pixels. Books with illustrations or uncertain OCR may retain original page images.

## Recovery and service control

```bash
# Stop discovering/dispatching. Detached workers finish their current books.
systemctl --user stop pdf-craft-cluster.service

# Reconnect to existing workers and continue discovery.
systemctl --user start pdf-craft-cluster.service

# Inspect failures before explicitly retrying books that exhausted three attempts.
.venv/bin/python -m pdf_craft_tool.cluster retry-failed \
  --config pdf-craft-output/cluster-config.json
```

Per-host setup and transfer errors are in `cluster/logs/bioNN.log`. Remote runtime
and job files live in `/scratch/pdf-craft-worker-mdra00001`, with current progress
in `worker-state.json`. SSH interruption preserves remote work. A worker crash is
recorded as a failed attempt and retried with backoff; existing completed page and
model-response caches are reused.

Do not put `queue.sqlite3` on `/home`: it is NFS. Do not delete running queue rows,
edit immutable release folders, or change installed model files mid-job.
The scheduler leaves other GPU processes running and waits for sufficient memory.

## Qwen3-VL experiment

`--vision-proofread` sends an image crop of each eligible paragraph along with its
OCR text. Use the explicit Instruct tag:

```bash
ollama pull qwen3-vl:4b-instruct

.venv/bin/python -m pdf_craft_tool book "sample_book/sarat upannays.pdf" \
  --work-dir pdf-craft-output/vision-evaluation --pages 4-13 \
  --tesseract /scratch/pdf-craft-worker-mdra00001/tesseract/bin/tesseract \
  --tessdata /scratch/pdf-craft-worker-mdra00001/tessdata \
  --model qwen3-vl:4b-instruct --vision-proofread --review-only
```

This example assumes an Ollama server with that model is already running. The
generic `qwen3-vl:4b` tag resolved to a Thinking model during the initial trial and
returned truncated/reasoning-only responses under our bounded JSON settings.
The explicit Instruct tag avoids that variant ambiguity. Review-only operation
records proposals without changing the reading copy. Tesseract crop corroboration
still governs automatic acceptance when review-only is omitted.

Qwen3-VL can read images, but that alone does not establish better Bengali accuracy.
Compare its proposals on the same reference passages before changing the fleet
profile. See [Qwen's model card](https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct)
and [Ollama model tags](https://ollama.com/library/qwen3-vl/tags).

The first explicit Instruct trial on Sarat pages 4–13 returned valid responses for
39 blocks and proposed no edits; one truncated response went to review. It therefore showed
no accuracy improvement over the 0.799% CER / 1.235% WER raw OCR baseline. It did
avoid the inappropriate historical-spelling edits seen in the earlier text-only
trial. That supports retaining it as an optional experiment, not a fleet accuracy
upgrade yet. The 4B Q4_K_M model ran within the 8 GB GPU budget.

## Reproducible deployment

The deployment seed contains `venv/`, `tesseract/`, `tessdata/`, `ollama/`, `models/`
and `tokenizer-cache/` at the same absolute path on every host. Provision it once
on bio10 using ordinary Python package dependencies and the required language/LLM
models. The coordinator copies it with rsync and validates imports and Tesseract.
Current setup uses Python 3.12 on matching Ubuntu hosts; it needs no PyTorch runtime.

`cluster init` freezes a code release and records the model digest and processing
profile. Scheduler-only upgrades preserve processing identity; OCR/proofreading
changes create a new profile. Stop dispatch before changing the active configuration
and ensure the systemd unit's working directory points to its new frozen release.

```bash
.venv/bin/python -m pdf_craft_tool.cluster init \
  --config pdf-craft-output/cluster-config.json \
  --repo /scratch/pdf-craft --data-root data \
  --state-root pdf-craft-output/cluster \
  --seed-root /scratch/pdf-craft-worker-mdra00001
```

For a separate visual evaluation fleet, use a separate configuration/state root,
install the explicit Instruct model in its seed, and pass
`--model qwen3-vl:4b-instruct --vision-proofread` to `init`. Visual fleet profiles
default to review-only. Do not launch two coordinators against the same worker
roots: the per-host lock prevents concurrent jobs, but one coordinator should own
the configured fleet.
