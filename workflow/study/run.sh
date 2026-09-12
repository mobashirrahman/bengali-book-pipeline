#!/usr/bin/env bash
# Reproducible entry point for the Phase-A transcript study workflow.
# Offline scaffold: no model execution, no network, no CUDA. Mirrors
# workflow/run.sh cache/PATH discipline so later execution rules inherit it.
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO_ROOT="$(pwd)"

export PIP_EXTRA_INDEX_URL="https://download.pytorch.org/whl/cu124"
MODELS_CACHE="${MODELS_CACHE:-$REPO_ROOT/models-cache}"
export HF_HOME="${HF_HOME:-$MODELS_CACHE/hf}"
export HF_HUB_CACHE="$HF_HOME/hub"
export MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-$MODELS_CACHE/datalab}"
export EASYOCR_MODEL_DIR="${EASYOCR_MODEL_DIR:-$MODELS_CACHE/easyocr}"
mkdir -p "$HF_HOME" "$MODEL_CACHE_DIR" "$EASYOCR_MODEL_DIR"

# Tooling: snakemake and conda are picked up from PATH by default; set
# SNAKEMAKE_BIN to an absolute path if your shell's PATH doesn't already
# resolve them. Never front-load another env's bin/ on PATH.
SNAKEMAKE_BIN="${SNAKEMAKE_BIN:-snakemake}"

exec "$SNAKEMAKE_BIN" -s workflow/study/Snakefile --sdm conda --cores "${CORES:-1}" "$@"
