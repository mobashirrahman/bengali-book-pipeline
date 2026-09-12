#!/usr/bin/env bash
# Reproducible entry point for the Phase-A transcript study workflow.
# Offline scaffold: no model execution, no network, no CUDA. Mirrors
# workflow/run.sh cache/PATH discipline so later execution rules inherit it.
set -euo pipefail
cd "$(dirname "$0")/../.."

export PIP_EXTRA_INDEX_URL="https://download.pytorch.org/whl/cu124"
export HF_HOME="/scratch/pdf-craft/models-cache/hf"
export HF_HUB_CACHE="$HF_HOME/hub"
export MODEL_CACHE_DIR="/scratch/pdf-craft/models-cache/datalab"
export EASYOCR_MODEL_DIR="/scratch/pdf-craft/models-cache/easyocr"
mkdir -p "$HF_HOME" "$MODEL_CACHE_DIR" "$EASYOCR_MODEL_DIR"

# Tooling: snakemake by absolute path; conda via condabin (which ships no
# python). Never front-load another env's bin/ on PATH.
export PATH="/home/mdra00001/miniforge3/condabin:/usr/bin:/bin"

exec /scratch/mdra00001/conda/envs/bnch-base/bin/snakemake -s workflow/study/Snakefile --sdm conda --cores "${CORES:-1}" "$@"
