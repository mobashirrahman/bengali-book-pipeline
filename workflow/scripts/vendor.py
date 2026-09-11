"""Load pdf_craft_tool.benchmark/external_eval without the heavy package.

Importing ``pdf_craft_tool`` normally executes its ``__init__`` (local book
pipeline: pdf_craft, pypdf, ...), which the scoring-only workflow envs must
not require. This loader executes just ``benchmark.py`` (pure stdlib +
rapidfuzz-at-call-time) and ``external_eval.py`` under a synthetic package.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_PKG = "wf_pdf_craft_tool"


def load():
    """Return the ``external_eval`` module (with ``benchmark`` as ``.benchmark``)."""
    if _PKG + ".external_eval" in sys.modules:
        return sys.modules[_PKG + ".external_eval"]
    root = Path(__file__).resolve().parents[2] / "pdf_craft_tool"
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(root)]
    sys.modules[_PKG] = pkg
    for name in ("benchmark", "external_eval"):
        qualname = f"{_PKG}.{name}"
        spec = importlib.util.spec_from_file_location(qualname, root / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualname] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return sys.modules[_PKG + ".external_eval"]
