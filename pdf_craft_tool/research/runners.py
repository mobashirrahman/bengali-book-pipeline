"""Frozen baseline runners with prediction caching and budgets (S4).

:func:`run_baselines` executes every registered adapter over inference-only
page dicts (``page_id``/``image_ref``/``ocr_text`` -- never gold) with a
shared :class:`PredictionCache`. A cache hit requires byte-identical
``model_id`` + ``prompt_hash`` + ``crop_hash`` + ``config_hash``; resume never
duplicates a row and never silently changes a prompt. Every adapter yields the
same output schema -- a failing adapter yields a ``Prediction`` carrying a
failure state, never a missing row -- until the budget is exhausted.

Stdlib and ``schema`` imports only. No model calls, downloads, CUDA, network
or PDF conversion in any code path: real inference additionally requires
``allow_execution=True`` plus a caller-supplied client transport.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import schema

TRACKED_FAILURE_STATES = ("ok", "unsupported", "empty", "truncated")


@dataclass(frozen=True)
class RunPlan:
    """Frozen plan for one baseline sweep."""

    study_id: str
    adapter_ids: tuple
    sample_manifest: str
    budget: dict

    def __post_init__(self):
        if not isinstance(self.study_id, str) or not self.study_id:
            raise schema.ContractError("study_id must be a non-empty string")
        if isinstance(self.adapter_ids, str) or not isinstance(
            self.adapter_ids, (list, tuple)
        ):
            raise schema.ContractError("adapter_ids must be a list or tuple")
        ids = tuple(self.adapter_ids)
        if not ids:
            raise schema.ContractError("adapter_ids must not be empty")
        for adapter_id in ids:
            if not isinstance(adapter_id, str) or not adapter_id:
                raise schema.ContractError(
                    "adapter_ids entries must be non-empty strings"
                )
        if len(set(ids)) != len(ids):
            raise schema.ContractError("adapter_ids must not contain duplicates")
        object.__setattr__(self, "adapter_ids", ids)
        if not isinstance(self.sample_manifest, str) or not self.sample_manifest:
            raise schema.ContractError("sample_manifest must be a non-empty string")
        if not isinstance(self.budget, dict):
            raise schema.ContractError("budget must be a dict")
        for key in ("max_model_calls", "max_wall_seconds"):
            if key not in self.budget:
                raise schema.ContractError(f"budget missing required key {key!r}")
            value = self.budget[key]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value < 0
            ):
                raise schema.ContractError(
                    f"budget[{key!r}] must be a non-negative number"
                )
        object.__setattr__(self, "budget", dict(self.budget))


class PredictionCache:
    """JSONL append-only cache keyed by (page_id, adapter identity hash).

    A hit requires byte-identical ``model_id`` + ``prompt_hash`` +
    ``crop_hash`` + ``config_hash``. Retry/resume never duplicates a row and
    never silently changes a prompt: :meth:`put` with an already-cached key
    is a no-op.
    """

    def __init__(self, path) -> None:
        self.path = Path(path)
        self._rows: dict[str, dict] = {}
        if self.path.exists():
            for lineno, line in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    raise schema.ContractError(
                        f"cannot parse cache line {lineno} in {self.path}: {exc}"
                    ) from exc
                if not isinstance(row, dict) or "key" not in row:
                    raise schema.ContractError(
                        f"invalid cache row on line {lineno} in {self.path}"
                    )
                if "prediction" in row and isinstance(row["prediction"], dict):
                    schema.assert_no_gold_fields(
                        row["prediction"], context="cached prediction"
                    )
                self._rows.setdefault(row["key"], row)

    def key(self, page_id: str, identity: dict) -> str:
        """Cache key binding a page to an exact adapter identity."""
        return schema.record_hash(
            {
                "page_id": page_id,
                "model_id": identity.get("model_id", ""),
                "prompt_hash": identity.get("prompt_hash", ""),
                "crop_hash": identity.get("crop_hash", ""),
                "config_hash": identity.get("config_hash", ""),
            }
        )

    def get(self, page_id: str, identity: dict) -> dict | None:
        """Return the cached prediction dict, or ``None`` on any mismatch."""
        row = self._rows.get(self.key(page_id, identity))
        if row is None or not isinstance(row.get("prediction"), dict):
            return None
        return dict(row["prediction"])

    def put(self, prediction: schema.Prediction, identity: dict) -> None:
        """Append one prediction row unless its key is already cached."""
        if not isinstance(prediction, schema.Prediction):
            raise schema.ContractError("prediction must be a schema.Prediction")
        key = self.key(prediction.page_id, dict(identity))
        if key in self._rows:
            return
        row = {
            "key": key,
            "page_id": prediction.page_id,
            "identity": dict(identity),
            "prediction": prediction.to_dict(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            )
        self._rows[key] = row


def _crop_hash_for(adapter, image_ref: str) -> str:
    helper = getattr(adapter, "crop_hash_for", None)
    if callable(helper):
        return helper(image_ref)
    ref_hash = schema.record_hash(image_ref) if image_ref else ""
    return schema.record_hash({"policy": "unknown", "image_ref": ref_hash})


def run_baselines(
    plan: RunPlan,
    *,
    pages: list[dict],
    adapters: dict,
    allow_execution: bool = False,
    clients: dict | None = None,
    cache: PredictionCache | None = None,
) -> dict:
    """Run every planned adapter over inference-only pages.

    ``pages`` entries hold ``page_id``/``image_ref``/``ocr_text`` only; any
    gold field raises ``schema.ContractError`` before invocation. Stops when
    the budget is exceeded and reports the partial result.
    """
    if not isinstance(plan, RunPlan):
        raise schema.ContractError("plan must be a RunPlan")
    if isinstance(pages, (dict, str, bytes)):
        raise schema.ContractError("pages must be a list of dicts")
    try:
        page_list = list(pages)
    except TypeError as exc:
        raise schema.ContractError(f"pages must be a list of dicts: {exc}") from exc
    if not isinstance(adapters, dict):
        raise schema.ContractError("adapters must be a dict")
    clients = dict(clients) if clients else {}

    max_calls = plan.budget["max_model_calls"]
    max_wall = plan.budget["max_wall_seconds"]
    started = time.monotonic()
    calls = 0
    exhausted = False
    predictions: list[dict] = []
    failures: list[dict] = []
    by_adapter = {
        adapter_id: {state: 0 for state in TRACKED_FAILURE_STATES}
        for adapter_id in plan.adapter_ids
    }

    for adapter_id in plan.adapter_ids:
        if adapter_id not in adapters:
            raise schema.ContractError(
                f"no adapter instance supplied for {adapter_id!r}"
            )
        adapter = adapters[adapter_id]
        for page in page_list:
            if exhausted:
                break
            if time.monotonic() - started > max_wall:
                exhausted = True
                break
            if not isinstance(page, dict):
                raise schema.ContractError("pages entries must be dicts")
            schema.assert_no_gold_fields(page, context="runner page input")
            if "page_id" not in page:
                raise schema.ContractError("pages entries need 'page_id'")
            page_id = page["page_id"]
            image_ref = page.get("image_ref", "")
            ocr_text = page.get("ocr_text", "")
            identity = dict(adapter.identity())
            identity["crop_hash"] = _crop_hash_for(adapter, image_ref)
            cached = cache.get(page_id, identity) if cache is not None else None
            if cached is not None:
                prediction = schema.Prediction.from_dict(cached)
            else:
                if calls >= max_calls:
                    exhausted = True
                    break
                calls += 1
                prediction = adapter.predict(
                    page_id=page_id,
                    image_ref=image_ref,
                    ocr_text=ocr_text,
                    allow_execution=allow_execution,
                    client=clients.get(adapter_id),
                )
                if cache is not None:
                    cache.put(prediction, identity)
            record = prediction.to_dict()
            predictions.append(record)
            state = record["failure_state"]
            bucket = by_adapter[adapter_id]
            bucket[state] = bucket.get(state, 0) + 1
            if state != "ok":
                failures.append(
                    {
                        "adapter_id": adapter_id,
                        "page_id": page_id,
                        "failure_state": state,
                        "reason": record["raw_output"][:200],
                    }
                )
        if exhausted:
            break
    return {
        "predictions": predictions,
        "failures": failures,
        "budget_exhausted": exhausted,
        "by_adapter": by_adapter,
    }
