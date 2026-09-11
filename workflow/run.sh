#!/usr/bin/env bash
# Reproducible entry point for the Bengali OCR benchmark.
# Caches live on /scratch (never $HOME/NFS); torch CUDA wheels resolve via
# the cu124 extra index (matches this host's 550-series driver).
set -euo pipefail
cd "$(dirname "$0")/.."

export PIP_EXTRA_INDEX_URL="https://download.pytorch.org/whl/cu124"
export HF_HOME="/scratch/pdf-craft/models-cache/hf"
export HF_HUB_CACHE="$HF_HOME/hub"
export MODEL_CACHE_DIR="/scratch/pdf-craft/models-cache/datalab"
export EASYOCR_MODEL_DIR="/scratch/pdf-craft/models-cache/easyocr"
export VLLM_DTYPE=float16
export VLLM_GPU_MEMORY_UTILIZATION=0.6
export SURYA_INFERENCE_PARALLEL=1
# Surya llamacpp backend: external llama-server (Vulkan) + GGUF from HF.
export SURYA_INFERENCE_BACKEND=llamacpp
export LLAMA_CPP_BINARY="/scratch/pdf-craft/models-cache/llamacpp/llama-b10883/llama-server"
export LD_LIBRARY_PATH="/scratch/pdf-craft/models-cache/llamacpp/llama-b10883${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
mkdir -p "$HF_HOME" "$MODEL_CACHE_DIR" "$EASYOCR_MODEL_DIR"

# Tooling: snakemake by absolute path; conda via condabin (which ships no
# python). Never front-load another env's bin/ on PATH: snakemake activates
# each rule's env via `source activate`, and a foreign python first on PATH
# silently wins over the activated env (verified 2026-09-10).
export PATH="/home/mdra00001/miniforge3/condabin:/usr/bin:/bin"

exec /scratch/mdra00001/conda/envs/bnch-base/bin/snakemake -s workflow/Snakefile --sdm conda --cores "${CORES:-1}" "$@"
