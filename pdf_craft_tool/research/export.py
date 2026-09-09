"""Reproducible export bundles for the Bengali-OCR research study (S7).

An export bundle is a self-contained directory copied out of a study root
(``pdf-craft-output/research/<study-id>/``) holding only records and assets
with a documented release decision. Two bundle kinds exist:

* ``inference``: public, model-facing artefacts. Gold fields must be absent
  from every record; any gold file, gold field, private source path, secret
  or hidden-test label raises :class:`ExportRefused`.
* ``gold``: scoring artefacts. Allowed only for items whose ``release_basis``
  is documented (never ``unknown`` / ``excluded``).

Only stdlib plus :mod:`pdf_craft_tool.research` imports. No network, models,
downloads, CUDA or PDF conversion in any code path.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import metrics, report, schema


class ExportRefused(RuntimeError):
    """Raised when a bundle cannot be built, verified or rebuilt safely."""


@dataclass(frozen=True)
class ExportItem:
    rel_path: str  # path inside the bundle, always under the bundle root
    sha256: str
    role: str  # "gold" | "inference" | "prediction" | "report" | "asset"
    release_basis: str  # from SourcePage.rights_basis / a documented decision


APPROVED_DECISIONS = frozenset({
    "approved", "released", "public_domain", "licensed", "owner_permission",
})
DOCUMENTED_BASES = frozenset({
    "approved", "released", "public_domain", "licensed", "owner_permission",
})
UNDOCUMENTED_BASES = frozenset({"", "unknown", "excluded", "unapproved"})

_PRIVATE_KEYS = frozenset({
    "source_path", "private_source", "secret", "api_key",
    "hidden_test", "test_label",
})
_ASSET_KEYS = frozenset({
    "asset", "asset_path", "assetpath", "image_path", "path",
    "source_path", "file", "file_path", "image_ref",
})

BUNDLE_MANIFEST_NAME = "bundle_manifest.json"
_RECORDS_NAME = "records.jsonl"


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _role_for(rel_path: str) -> str:
    name = rel_path.lower()
    if "gold" in name:
        return "gold"
    if "predict" in name:
        return "prediction"
    if "sample" in name or "manifest" in name or "inference" in name:
        return "inference"
    if name.endswith(".md") or "report" in name or "table" in name \
            or "evaluat" in name:
        return "report"
    return "asset"


def _decision_of(entry) -> tuple[bool, str, str]:
    """Return (approved, release_basis, reason) for a release_decisions entry."""
    if entry is None:
        return False, "unknown", "no release decision recorded"
    if isinstance(entry, str):
        if entry in APPROVED_DECISIONS:
            return True, entry, ""
        return False, entry or "unknown", f"release decision {entry!r}"
    if isinstance(entry, dict):
        status = str(entry.get("status", entry.get("decision", "approved")))
        if entry.get("approved") is False or status in (
                "unapproved", "denied", "missing", ""):
            return False, status or "unknown", \
                f"release decision {status!r}"
        basis = str(entry.get("release_basis",
                              entry.get("rights_basis", status)))
        if status in APPROVED_DECISIONS or entry.get("approved") is True:
            return True, basis or "unknown", ""
        return False, basis or "unknown", f"release decision {status!r}"
    return False, "unknown", f"unreadable release decision {entry!r}"


def _check_asset_ref(value: str, *, study_root: Path, where: str) -> None:
    if not isinstance(value, str) or not value:
        return
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        normalised = os.path.normpath(os.path.join(str(study_root), value))
        try:
            inside = os.path.commonpath([str(study_root), normalised]) \
                == str(study_root)
        except ValueError:
            inside = False
        if not inside:
            raise ExportRefused(
                f"asset reference {value!r} in {where} resolves outside "
                f"the study root"
            )


def _scan_record(record: dict, *, study_root: Path, where: str,
                 kind: str) -> None:
    """Enforce gold-free / secret-free / in-root rules on one record dict."""
    if not isinstance(record, dict):
        return
    if kind == "inference":
        try:
            schema.assert_no_gold_fields(record, context=f"inference {where}")
        except schema.ContractError as exc:
            raise ExportRefused(
                f"gold field in inference bundle ({where}): {exc}"
            ) from exc
    stack = [record]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                lowered = str(key).lower()
                if kind == "inference" and lowered in _PRIVATE_KEYS:
                    raise ExportRefused(
                        f"private field {key!r} must never enter an "
                        f"inference bundle ({where})"
                    )
                if lowered in _ASSET_KEYS:
                    _check_asset_ref(value, study_root=study_root,
                                     where=f"{where}:{key}")
                stack.append(value)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)


def _records_in_file(path: Path, *, study_root: Path, where: str,
                     kind: str) -> None:
    """Parse a JSON/JSONL file and scan every record it holds."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExportRefused(f"cannot read {where}: {exc}") from exc
    suffix = path.suffix.lower()
    try:
        if suffix == ".jsonl":
            payloads = [json.loads(line) for line in text.splitlines()
                        if line.strip()]
        else:
            payloads = [json.loads(text)]
    except ValueError as exc:
        # Non-JSON assets (images, text) carry no scannable records.
        return
    for payload in payloads:
        if isinstance(payload, dict) and isinstance(
                payload.get("records"), list):
            for record in payload["records"]:
                if isinstance(record, dict):
                    _scan_record(record, study_root=study_root,
                                 where=where, kind=kind)
        elif isinstance(payload, dict):
            _scan_record(payload, study_root=study_root,
                         where=where, kind=kind)
        elif isinstance(payload, list):
            for record in payload:
                if isinstance(record, dict):
                    _scan_record(record, study_root=study_root,
                                 where=where, kind=kind)


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def build_bundle(*, study_root, out_dir, kind: str,
                 release_decisions: dict) -> dict:
    """Copy approved study files into a checksummed, self-describing bundle.

    ``kind`` is ``'inference'`` or ``'gold'``. Files without an approving
    entry in ``release_decisions`` are excluded with a recorded reason;
    anything that would leak gold, secrets or out-of-root paths raises
    :class:`ExportRefused` instead of being silently dropped.
    """
    if kind not in ("inference", "gold"):
        raise ExportRefused(
            f"unknown bundle kind {kind!r}; expected 'inference' or 'gold'"
        )
    root = Path(study_root)
    dest_root = Path(out_dir)
    if not root.is_dir():
        raise ExportRefused(f"study root {root} is not a directory")
    if not isinstance(release_decisions, dict):
        raise ExportRefused("release_decisions must be a dict")
    try:
        root_resolved = root.resolve()
        dest_resolved = dest_root.resolve() \
            if dest_root.exists() else dest_root.absolute()
    except OSError as exc:
        raise ExportRefused(f"cannot resolve bundle paths: {exc}") from exc

    items: list[dict] = []
    excluded: list[dict] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() and not path.exists():
            continue
        try:
            if dest_root.exists() and path.resolve().is_relative_to(
                    dest_resolved):
                continue
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        approved, basis, reason = _decision_of(
            release_decisions.get(rel))
        if not approved:
            excluded.append({"rel_path": rel, "reason": reason})
            continue
        role = _role_for(rel)
        if kind == "inference" and role == "gold":
            raise ExportRefused(
                f"gold file {rel!r} must not enter an inference bundle"
            )
        if kind == "gold" and basis in UNDOCUMENTED_BASES:
            raise ExportRefused(
                f"gold bundle item {rel!r} has no documented release_basis "
                f"(got {basis!r})"
            )
        if path.suffix.lower() in (".json", ".jsonl"):
            _records_in_file(path, study_root=root_resolved,
                             where=rel, kind=kind)
        data = path.read_bytes()
        digest = _sha256_bytes(data)
        target = dest_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        reread = _sha256_bytes(target.read_bytes())
        if reread != digest:
            raise ExportRefused(
                f"checksum mismatch after writing {rel!r}"
            )
        items.append(asdict(ExportItem(rel_path=rel, sha256=digest,
                                       role=role,
                                       release_basis=basis)))

    manifest = {
        "kind": kind,
        "study_root": str(root),
        "items": items,
        "excluded": excluded,
    }
    manifest_path = dest_root / BUNDLE_MANIFEST_NAME
    dest_root.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n", encoding="utf-8")
    return {"bundle_manifest": str(manifest_path), "items": items,
            "excluded": excluded}


def verify_bundle(bundle_dir) -> dict:
    """Recompute every checksum; refuse mismatches or gold in inference."""
    root = Path(bundle_dir)
    manifest_path = root / BUNDLE_MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExportRefused(
            f"cannot load bundle manifest {manifest_path}: {exc}") from exc
    kind = manifest.get("kind", "inference")
    items = manifest.get("items", [])
    for item in items:
        rel = item["rel_path"]
        target = root / rel
        try:
            resolved = target.resolve()
        except OSError as exc:
            raise ExportRefused(
                f"cannot resolve bundle item {rel!r}: {exc}") from exc
        try:
            inside = resolved.is_relative_to(root.resolve())
        except (AttributeError, OSError):
            inside = os.path.commonpath(
                [str(root.resolve()), str(resolved)]) == str(root.resolve())
        if not inside:
            raise ExportRefused(
                f"bundle item {rel!r} escapes the bundle root")
        if not target.is_file():
            raise ExportRefused(f"bundle item {rel!r} is missing")
        digest = _sha256_bytes(target.read_bytes())
        if digest != item["sha256"]:
            raise ExportRefused(
                f"checksum mismatch for bundle item {rel!r}")
        if kind == "inference" and target.suffix.lower() in (
                ".json", ".jsonl"):
            _records_in_file(target, study_root=root.resolve(),
                             where=rel, kind="inference")
    return {"ok": True, "kind": kind, "items": len(items),
            "excluded": len(manifest.get("excluded", []))}


def rebuild_tables_offline(bundle_dir) -> dict:
    """Rebuild report tables from the bundle's saved predictions alone.

    Reads the bundle's ``records.jsonl`` (scored-record rows written by the
    ``evaluate`` step) and recomputes the baseline table with the versioned
    scorer. No network, no re-inference, no gold re-entry.
    """
    root = Path(bundle_dir)
    records_path = root / _RECORDS_NAME
    if not records_path.is_file():
        found = sorted(root.rglob(_RECORDS_NAME))
        if not found:
            raise ExportRefused(
                f"bundle {root} holds no {_RECORDS_NAME} to rebuild from")
        records_path = found[0]
    try:
        rows = [json.loads(line) for line in
                records_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except (OSError, ValueError) as exc:
        raise ExportRefused(
            f"cannot load bundle records {records_path}: {exc}") from exc
    arms = sorted({row.get("arm_id", "") for row in rows if row.get("arm_id")})
    if not arms:
        raise ExportRefused(f"bundle records {records_path} name no arms")
    config = {
        "study_id": "bundle-offline-rebuild",
        "arms": arms,
        "expected_records": len(rows),
        "total_pages": len(rows),
    }
    with tempfile.TemporaryDirectory(prefix="research-bundle-") as tmp:
        config_path = Path(tmp) / "bundle_config.json"
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n", encoding="utf-8")
        rebuilt = report.rebuild_from_records(records_path, config=config_path)
    n_records = rebuilt.get("header", {}).get("n_records", len(rows))
    report.reconcile(rebuilt, expected_records=n_records)
    return {"baseline": rebuilt["baseline"], "n_records": n_records,
            "records": str(records_path),
            "metric_version": metrics.METRIC_VERSION}
