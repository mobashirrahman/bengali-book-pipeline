#!/usr/bin/env bash
# Reproducible entry point for the Bengali OCR benchmark.
# Caches default under the repo root, overridable via env vars below;
# torch CUDA wheels resolve via the cu124 extra index (match this to your
# driver's CUDA compatibility if it's not a 550-series driver).
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

export PIP_EXTRA_INDEX_URL="https://download.pytorch.org/whl/cu124"
MODELS_CACHE="${MODELS_CACHE:-$REPO_ROOT/models-cache}"
export HF_HOME="${HF_HOME:-$MODELS_CACHE/hf}"
export HF_HUB_CACHE="$HF_HOME/hub"
export MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-$MODELS_CACHE/datalab}"
export EASYOCR_MODEL_DIR="${EASYOCR_MODEL_DIR:-$MODELS_CACHE/easyocr}"
export VLLM_DTYPE=float16
export VLLM_GPU_MEMORY_UTILIZATION=0.6
export SURYA_INFERENCE_PARALLEL=1
# Surya llamacpp backend: external llama-server (Vulkan) + GGUF from HF.
# Set LLAMA_CPP_BINARY to your llama-server build if using this backend.
export SURYA_INFERENCE_BACKEND=llamacpp
export LLAMA_CPP_BINARY="${LLAMA_CPP_BINARY:-$MODELS_CACHE/llamacpp/llama-b10883/llama-server}"
export LD_LIBRARY_PATH="$(dirname "$LLAMA_CPP_BINARY")${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
mkdir -p "$HF_HOME" "$MODEL_CACHE_DIR" "$EASYOCR_MODEL_DIR"

# Tooling: snakemake and conda are picked up from PATH by default; set
# SNAKEMAKE_BIN to an absolute path if your shell's PATH doesn't already
# resolve them (e.g. cron, or a conda install with no shell init sourced).
# Never front-load another env's bin/ on PATH: snakemake activates each
# rule's env via `source activate`, and a foreign python first on PATH
# silently wins over the activated env (verified 2026-09-10).
SNAKEMAKE_BIN="${SNAKEMAKE_BIN:-snakemake}"

exec "$SNAKEMAKE_BIN" -s workflow/Snakefile --sdm conda --cores "${CORES:-1}" "$@"
