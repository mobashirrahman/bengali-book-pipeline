# Cluster pipeline handoff

This file is for agents maintaining the running deployment. Read the live
configuration and status before changing anything; host health and input counts
change over time. User operating instructions are in
`docs/en/CLUSTER_PIPELINE.md`; single-book details are in
`docs/en/BENGALI_BOOK_PIPELINE.md`.

## User authorization and current design

The user authorized bio01–bio24 for continuous OCR and proofreading of the growing
`/scratch/pdf-craft/data` collection. The coordinator is a user systemd service on
bio10. One job runs per available GPU host. Books, rather than neural-model layers,
are distributed across servers. CPU Tesseract and GPU proofreading run sequentially
within a book job; other servers process different books concurrently.

Default production profile: Tesseract 5.5.3, tessdata_best Bengali, Qwen3.5-4B
through Ollama, automatic corrections only after source-crop Tesseract corroboration,
then EPUB/Markdown/chunks. Raw and proofread `.pcex` artifacts remain separate.
Qwen3-VL is optional and under evaluation, not automatically chained after Qwen3.5.
Use `qwen3-vl:4b-instruct` for bounded visual proofreading. The generic
`qwen3-vl:4b` tag resolved to a Thinking variant in this environment and failed the
bounded JSON trial; do not treat reasoning-channel text as an accepted answer.

## Live paths

- Repository/data: `/scratch/pdf-craft` on bio10 only.
- Config: `/scratch/pdf-craft/pdf-craft-output/cluster-config.json`.
- Local queue: `/scratch/pdf-craft/pdf-craft-output/cluster/queue.sqlite3`.
- Summary: same directory, `status.json`; query the CLI for current database state.
- Coordinator log: same directory, `coordinator.log`.
- Host provisioning/transfer logs: same directory, `logs/bioNN.log`.
- Results: same directory, `jobs/<job-id>/summary.json` and `work/`.
- Immutable source snapshots: same directory, `releases/<release-hash>/`.
- Per-node runtime and work: `/scratch/pdf-craft-worker-mdra00001/`.
- Per-node current state: `worker-state.json`; job logs/results under `jobs/<id>/`.
- Service: `pdf-craft-cluster.service`, linked to the unit file in `pdf-craft-output/`.
- User lingering was already enabled, so the user service survives logout.

Read the config to find the actual active release and model digest. Do not edit
immutable release copies or mutate a deployed model under a running job.

## Modules and invariants

- `pdf_craft_tool/cluster_queue.py`: coordinator-local SQLite schema, stability
  observations, content hashing, profile identities, atomic claims and retries.
- `pdf_craft_tool/cluster.py`: discovery loop, SSH deployment, worker polling,
  transfers, result verification and coordinator lifecycle.
- `pdf_craft_tool/cluster_worker.py`: detached worker process, per-host flock,
  private loopback Ollama, heartbeats and durable per-attempt results.
- `pdf_craft_tool/vision.py`: original paragraph image crops for optional visual
  proofreading. Image use must be reflected in the model/cache identity.
- `pdf_craft_tool/book.py`: existing single-book orchestration and stage caches.
- `pdf_craft/transformer/proofreader.py`: exact-span safety checks and audit records.
- `tests/test_cluster.py`: discovery, duplicates, atomic ownership, retries,
  restart behavior, filename handling and actual image attachment.

Queue identity is PDF content SHA-256 plus processing profile. A new path containing
identical bytes reuses that job; changed content gets a new job. Observe a file
unchanged for 120 seconds before queueing. Workers recheck the transferred hash.
Hashing must not hold the SQLite writer lock. Never expire a running assignment
just because SSH is unavailable: the remote worker may still be processing it.
Results are accepted only for the assigned host and attempt. Older failed attempt
state must not immediately fail a newly retried attempt. Mark completion only
after copying and verifying reader artifacts on bio10.

SQLite is on local ext4, not `/home` NFS. Each thread opens its own connection.
`BEGIN IMMEDIATE` serializes claims. A separate coordinator flock prevents duplicate
schedulers. A worker inherits its flock descriptor before the start command exits,
avoiding two simultaneous GPU jobs. SSH commands use `shlex.join`; rsync uses
protected arguments. PDFs and results may contain Bengali names or shell characters.

The first job uses at least 5500 MiB free GPU memory and <=10% utilization as an
admission check. Existing unrelated GPU work is left running. Do not repair drivers,
reboot machines, change SSH authentication, or kill unrelated processes to enlarge
the pool. Previously unseen SSH host keys use normal accept-new enrollment; changed
keys remain rejected. The scheduler continues retrying unavailable hosts.

Stopping the coordinator stops discovery/dispatch, while detached workers finish
their books. Restarting reconnects to their status and collects results. Do not
clear running rows to make the queue look idle. Failed jobs back off and stop after
three attempts; `retry-failed` is an explicit retry operation.

## Deployment and verification notes

Publication enrichment lives in `renderer/epub/{options,publication,validation}.py`
and `pdf_craft_tool/{book_metadata,publish}.py`; see `docs/en/PUBLICATION.md`.
`pdf_craft_tool/publication_queue.py` now runs as the separate user service
`pdf-craft-publications.service` on bio10, with its own immutable snapshot under
`pdf-craft-output/publication-releases/`. Inspect the linked service unit for the
actual release. It reads the OCR SQLite queue read-only and publishes collected
`.pcex` files locally, with EPUBCheck enabled. It does not replace worker releases.
Its output, per-job records, metadata overrides and scan status live under
`cluster/publications/`; the log is `cluster/publications.log`. A separate flock
prevents duplicate publishers. Editing metadata waits 120 seconds, then creates a
new publication revision; errors back off five minutes. Preserve prior revisions.
Central `publications/metadata/<job-id>.json` overrides adjacent PDF sidecars.
Neither is written by the publisher. Worker transfers need no sidecar changes.
Renderer source changes still affect the OCR fleet processing hash; do not
requeue the entire collection's OCR merely to update covers or metadata.

Runtime seed uses Python 3.12, ordinary package dependencies (no PyTorch required),
Tesseract, Ollama, Bengali model data, cached tokenizer data and Qwen3.5-4B.
Identical absolute runtime paths make venv and conda runtime copies usable on the
matching Ubuntu hosts. Host runtime validation precedes job admission.

On the initial audit, bio01/05/07/08/09/10/11/14/15/16/17/18/19/20/21 were healthy;
bio04 was reachable but had a broken NVIDIA driver. Some initially unavailable
hosts may recover later. Always query live state rather than relying on this list.

Earlier ten-page Sarat benchmark: raw CER 0.799%, WER 1.235% against the imperfect
EPUB reference. Qwen3.5 text-only edits initially worsened it; guarded edits preserved
the raw score and applied no changes across the 50-page test. Do not claim an LLM
accuracy gain. Vision results must be evaluated separately against identical
reference spans. Reference text never enters OCR or model prompts.

At handoff, inspect live status and model audit files for the latest trial results.
Full collection completion is asynchronous and is not implied by service health.

The explicit VL Instruct trial on Sarat pages 4–13 finished: 39 valid responses,
zero proposals, one truncated response routed to review. Audit directory:
`pdf-craft-output/book-pipeline-sarat-10/proofread/338009c309dbe6421e16/audit/`.
Its model digest starts `ee4b975b58c1`; the complete digest is in stage settings.
No demonstrated accuracy gain; production remains Qwen3.5 with crop corroboration.
