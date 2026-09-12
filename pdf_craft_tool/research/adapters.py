"""Frozen baseline recogniser/corrector adapters (S4).

Every adapter exposes the same interface and either produces a
``schema.Prediction`` or a clear failure. Offline runs (``allow_execution=False``
or no backing model) degrade to ``failure_state='unsupported'`` -- never a
placeholder text. Only B0 (unchanged production Tesseract configuration)
produces a real prediction with no model.

No model calls, downloads, CUDA or network occur in any code path reachable
with ``allow_execution=False``. Real inference additionally requires an
explicit caller-supplied ``client`` transport. Module top level imports only
stdlib and ``schema``; the existing ``ConservativeProofreader`` is wrapped
behind a lazy import inside :class:`ExistingPolicyAdapter`.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import schema

CROP_POLICIES = ("none", "page_image", "line_crop")
ADAPTER_KINDS = ("recognizer", "corrector")


class AdapterUnavailable(RuntimeError):
    """Raised when an adapter id has no registered implementation."""


@dataclass(frozen=True)
class AdapterSpec:
    """Frozen identity of one baseline arm (B0..B5 or an ablation)."""

    adapter_id: str
    kind: str
    model_id: str
    prompt_id: str
    prompt_hash: str
    crop_policy: str
    config: dict = field(default_factory=dict)

    def __post_init__(self):
        for name in ("adapter_id", "model_id", "prompt_id", "prompt_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise schema.ContractError(
                    f"AdapterSpec.{name} must be a non-empty string, "
                    f"got {value!r}"
                )
        if self.kind not in ADAPTER_KINDS:
            raise schema.ContractError(
                f"AdapterSpec.kind must be one of {list(ADAPTER_KINDS)}, "
                f"got {self.kind!r}"
            )
        if self.crop_policy not in CROP_POLICIES:
            raise schema.ContractError(
                f"AdapterSpec.crop_policy must be one of {list(CROP_POLICIES)}, "
                f"got {self.crop_policy!r}"
            )
        if not isinstance(self.config, dict):
            raise schema.ContractError("AdapterSpec.config must be a dict")
        object.__setattr__(self, "config", dict(self.config))

    def config_hash(self) -> str:
        """Identity hash over the frozen fields (key order independent)."""
        return schema.record_hash(
            {
                "adapter_id": self.adapter_id,
                "kind": self.kind,
                "model_id": self.model_id,
                "prompt_id": self.prompt_id,
                "prompt_hash": self.prompt_hash,
                "crop_policy": self.crop_policy,
                "config": dict(self.config),
            }
        )


class BaseAdapter:
    """Shared guard/crop/prediction plumbing for every baseline adapter."""

    spec: AdapterSpec

    def __init__(self, spec: AdapterSpec) -> None:
        if not isinstance(spec, AdapterSpec):
            raise schema.ContractError(
                f"spec must be an AdapterSpec, got {type(spec)}"
            )
        self.spec = spec

    # -- identity ------------------------------------------------------
    def identity(self) -> dict:
        """Static adapter identity (per-page ``crop_hash`` added by caller)."""
        return {
            "adapter_id": self.spec.adapter_id,
            "model_id": self.spec.model_id,
            "prompt_hash": self.spec.prompt_hash,
            "crop_policy": self.spec.crop_policy,
            "crop_hash_policy": self.spec.crop_policy,
            "config_hash": self.spec.config_hash(),
        }

    def crop_hash_for(self, image_ref: str = "") -> str:
        """Crop identity actually used for one page image reference."""
        ref_hash = schema.record_hash(image_ref) if image_ref else ""
        return schema.record_hash(
            {"policy": self.spec.crop_policy, "image_ref": ref_hash}
        )

    def crop_identity_for(self, image_ref: str = "") -> dict:
        """Crop description handed to the model client (spy-visible)."""
        ref_hash = schema.record_hash(image_ref) if image_ref else ""
        return {
            "policy": self.spec.crop_policy,
            "image_ref_hash": ref_hash,
            "crop_hash": schema.record_hash(
                {"policy": self.spec.crop_policy, "image_ref": ref_hash}
            ),
        }

    # -- prediction entry point ----------------------------------------
    def predict(
        self,
        *,
        page_id: str,
        image_ref: str = "",
        ocr_text: str = "",
        allow_execution: bool = False,
        client=None,
        **kwargs,
    ) -> schema.Prediction:
        """Produce a ``schema.Prediction``; never a placeholder text.

        ``image_ref``/``ocr_text`` are inference-only inputs. Any gold field
        passed anywhere raises ``schema.ContractError`` before any work
        (including before a client is invoked).
        """
        schema.assert_no_gold_fields(
            {
                "page_id": page_id,
                "image_ref": image_ref,
                "ocr_text": ocr_text,
                **kwargs,
            },
            context=f"adapter {self.spec.adapter_id} inference input",
        )
        raw_output, parsed_text, failure_state = self._run(
            page_id=page_id,
            image_ref=image_ref,
            ocr_text=ocr_text,
            allow_execution=allow_execution,
            client=client,
        )
        return schema.Prediction(
            page_id=page_id,
            system_id=self.spec.adapter_id,
            config_hash=self.spec.config_hash(),
            model_id=self.spec.model_id,
            prompt_hash=self.spec.prompt_hash,
            crop_hash=self.crop_hash_for(image_ref),
            raw_output=raw_output,
            parsed_text=parsed_text,
            failure_state=failure_state,
            timing_ms=0,
            resource={},
        )

    def _run(
        self, *, page_id, image_ref, ocr_text, allow_execution, client
    ) -> tuple[str, str, str]:
        raise NotImplementedError

    # -- client plumbing -------------------------------------------------
    def _unsupported(self, reason: str) -> tuple[str, str, str]:
        return (reason, "", "unsupported")

    def _call_client(self, client, *, page_id, ocr_text, crop) -> tuple[str, str, str]:
        """Invoke a caller-supplied (fake in tests) transport and wrap output."""
        try:
            try:
                output = client(
                    text=ocr_text,
                    crop=crop,
                    page_id=page_id,
                    adapter_id=self.spec.adapter_id,
                )
            except TypeError:
                output = client(ocr_text)
        except Exception as exc:
            return (f"client invocation failed: {exc}", "", "parse_error")
        return _wrap_client_output(output)


def _wrap_client_output(output) -> tuple[str, str, str]:
    """Normalise a client return value to (raw_output, parsed_text, state)."""
    if output is None:
        return ("client returned None", "", "parse_error")
    if isinstance(output, str):
        if not output.strip():
            return (output, output, "empty")
        return (output, output, "ok")
    if isinstance(output, dict):
        raw = output.get("raw_output", "")
        parsed = output.get("parsed_text", raw)
        state = output.get("failure_state", "ok")
        if output.get("truncated") is True:
            state = "truncated"
        if not isinstance(raw, str) or not isinstance(parsed, str):
            return ("client returned non-string output", "", "parse_error")
        try:
            state = schema.FailureState.coerce(state).value
        except schema.ContractError:
            return (raw, parsed, "parse_error")
        if state == "ok" and not parsed.strip():
            state = "empty"
        return (raw, parsed, state)
    return ("client returned unreadable output", "", "parse_error")


class UnchangedTesseractAdapter(BaseAdapter):
    """B0 -- returns ``ocr_text`` verbatim; the only model-free prediction."""

    def _run(
        self, *, page_id, image_ref, ocr_text, allow_execution, client
    ) -> tuple[str, str, str]:
        if not isinstance(ocr_text, str):
            raise schema.ContractError("ocr_text must be a string")
        if not ocr_text.strip():
            return (ocr_text, ocr_text, "empty")
        return (ocr_text, ocr_text, "ok")


class UnavailableRecognizerAdapter(BaseAdapter):
    """B1/B2 default -- always ``unsupported``; records the reason."""

    def _run(
        self, *, page_id, image_ref, ocr_text, allow_execution, client
    ) -> tuple[str, str, str]:
        reason = self.spec.config.get("status", "unavailable")
        return self._unsupported(
            f"recognizer {self.spec.adapter_id} ({self.spec.model_id}) "
            f"unavailable: {reason}"
        )


class TextOnlyCorrectionAdapter(BaseAdapter):
    """B3 -- text-only correction; needs an explicit client transport."""

    def _run(
        self, *, page_id, image_ref, ocr_text, allow_execution, client
    ) -> tuple[str, str, str]:
        if not allow_execution:
            return self._unsupported(
                f"corrector {self.spec.adapter_id} gated: allow_execution=False"
            )
        if client is None:
            return self._unsupported(
                f"corrector {self.spec.adapter_id} has no client transport"
            )
        return self._call_client(
            client,
            page_id=page_id,
            ocr_text=ocr_text,
            crop=self.crop_identity_for(image_ref),
        )


class ExistingPolicyAdapter(BaseAdapter):
    """B4 -- existing conservative exact-span + Tesseract-crop gate.

    Wraps ``pdf_craft.transformer.proofreader.ConservativeProofreader``
    behind a lazy import guarded by ``allow_execution``. Offline (or with no
    client transport) it degrades to ``unsupported`` and never touches the
    filesystem, models or network.
    """

    def _run(
        self, *, page_id, image_ref, ocr_text, allow_execution, client
    ) -> tuple[str, str, str]:
        if not allow_execution:
            return self._unsupported(
                f"corrector {self.spec.adapter_id} gated: allow_execution=False"
            )
        if client is None:
            return self._unsupported(
                f"corrector {self.spec.adapter_id} has no client transport"
            )
        try:
            from pdf_craft.transformer.proofreader import (  # noqa: F401
                ConservativeProofreader,
            )
        except Exception as exc:
            return self._unsupported(
                f"corrector {self.spec.adapter_id} policy unavailable: {exc}"
            )
        return self._call_client(
            client,
            page_id=page_id,
            ocr_text=ocr_text,
            crop=self.crop_identity_for(image_ref),
        )


class EvidenceGateAdapter(BaseAdapter):
    """B5 -- evidence-based gate with abstention (pending calibration)."""

    def _run(
        self, *, page_id, image_ref, ocr_text, allow_execution, client
    ) -> tuple[str, str, str]:
        return self._unsupported(
            f"corrector {self.spec.adapter_id} ({self.spec.model_id}) "
            "pending calibration: abstains offline"
        )


# Names of env vars whose VALUES are secrets and must never surface in a
# Prediction field (keys) or be linkable (endpoint hosts).
_SECRET_ENV_NAMES = (
    "GOOGLE_VISION_API_KEY",
    "AZURE_VISION_KEY",
    "AZURE_VISION_ENDPOINT",
)


def _redact_text(text: str, *, env=None) -> str:
    """Return ``text`` with every secret value and endpoint host stripped."""
    source = env if env is not None else os.environ
    try:
        values = [source.get(name, "") for name in _SECRET_ENV_NAMES]
    except Exception:
        values = []
    redacted = text if isinstance(text, str) else str(text)
    for value in values:
        if value:
            redacted = redacted.replace(value, "[redacted]")
            host = _host_of(value)
            if host:
                redacted = redacted.replace(host, "[redacted-host]")
    return redacted


def _host_of(value: str) -> str:
    """Extract the host part of a URL-ish value ("" when not URL-like)."""
    text = value.strip()
    for scheme in ("https://", "http://"):
        if text.lower().startswith(scheme):
            rest = text[len(scheme):]
            return rest.split("/", 1)[0].split("@")[-1].split(":", 1)[0]
    return ""


def _numeric_resource_value(value):
    """Coerce one resource value to the numeric-only schema contract.

    ``schema.Prediction.resource`` accepts int/float values only, so string
    provenance (endpoint sha, api/model versions) is bound through
    ``config_hash``/``raw_output`` instead and only numeric projections land
    here. Bools become 0/1; anything else non-numeric is dropped (None).
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    return None


class VendorOCRAdapter(BaseAdapter):
    """Base class for pay-per-call vendor OCR recognizers (GV/AZ).

    Gated exactly like :class:`TextOnlyCorrectionAdapter`: with
    ``allow_execution=False`` or no client transport the adapter yields
    ``unsupported`` without touching the filesystem or network. When allowed
    it reads image bytes from ``image_ref`` and delegates to the
    caller-supplied client
    ``client(image_bytes=..., page_id=..., adapter_id=..., config=...)``.

    ``spec.config`` must pin ``endpoint_env`` (env-var name, or a literal
    public endpoint URL for Google), ``api_version``, ``model_version`` and
    ``drift_epoch`` (YYYY-MM-DD the spec was frozen). The runtime endpoint
    string is resolved from the environment at call time; its sha256
    ("" when unset) is folded into both ``identity()["config_hash"]`` and the
    Prediction's ``config_hash`` so a changed endpoint is a cache miss. The
    per-call date is provenance only (``resource["call_date"]`` as a YYYYMMDD
    int -- ``schema.Prediction.resource`` is numeric-only); version splits
    happen by bumping ``drift_epoch`` explicitly, so reruns reproduce from
    cache.
    """

    REQUIRED_CONFIG = ("endpoint_env", "api_version", "model_version",
                       "drift_epoch")

    # -- endpoint identity ---------------------------------------------
    def _runtime_endpoint(self) -> str:
        """Resolve the endpoint string from the environment at call time."""
        marker = self.spec.config.get("endpoint_env", "")
        if not isinstance(marker, str) or not marker:
            return ""
        lowered = marker.lower()
        if lowered.startswith("https://") or lowered.startswith("http://"):
            return marker  # literal public endpoint (Google)
        return os.environ.get(marker, "")

    def _endpoint_sha256(self) -> str:
        endpoint = self._runtime_endpoint()
        if not endpoint:
            return ""
        return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()

    def identity(self) -> dict:
        base = super().identity()
        folded = schema.record_hash(
            {
                "base": base["config_hash"],
                "endpoint_sha256": self._endpoint_sha256(),
            }
        )
        base["config_hash"] = folded
        return base

    # -- prediction entry point ----------------------------------------
    def predict(
        self,
        *,
        page_id: str,
        image_ref: str = "",
        ocr_text: str = "",
        allow_execution: bool = False,
        client=None,
        **kwargs,
    ) -> schema.Prediction:
        started = time.perf_counter()
        prediction = super().predict(
            page_id=page_id,
            image_ref=image_ref,
            ocr_text=ocr_text,
            allow_execution=allow_execution,
            client=client,
            **kwargs,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        resource = self._provenance_resource()
        return dataclasses.replace(
            prediction,
            config_hash=self.identity()["config_hash"],
            timing_ms=max(0, elapsed_ms),
            resource=resource,
        )

    def _provenance_resource(self) -> dict:
        """Numeric provenance for the last :meth:`_run` call (schema-safe)."""
        client_resource = getattr(self, "_last_client_resource", {})
        merged: dict = {}
        if isinstance(client_resource, dict):
            for key, value in client_resource.items():
                if not isinstance(key, str) or not key:
                    continue
                coerced = _numeric_resource_value(value)
                if coerced is not None:
                    merged[key] = coerced
        today = datetime.datetime.now(datetime.timezone.utc).date()
        merged["call_date"] = int(today.strftime("%Y%m%d"))
        merged["version_mismatch"] = int(
            bool(getattr(self, "_last_version_mismatch", False))
        )
        return merged

    # -- client plumbing -------------------------------------------------
    def _run(
        self, *, page_id, image_ref, ocr_text, allow_execution, client
    ) -> tuple[str, str, str]:
        self._last_client_resource = {}
        self._last_version_mismatch = False
        if not allow_execution:
            return self._unsupported(
                f"recognizer {self.spec.adapter_id} gated: allow_execution=False"
            )
        if client is None:
            return self._unsupported(
                f"recognizer {self.spec.adapter_id} has no client transport"
            )
        if not isinstance(image_ref, str) or not image_ref:
            return ("no image_ref for vendor OCR call", "", "invocation_error")
        try:
            image_bytes = Path(image_ref).read_bytes()
        except (OSError, ValueError) as exc:
            return (
                _redact_text(f"cannot read image_ref: {type(exc).__name__}: {exc}"),
                "",
                "invocation_error",
            )
        try:
            output = client(
                image_bytes=image_bytes,
                page_id=page_id,
                adapter_id=self.spec.adapter_id,
                config=dict(self.spec.config),
            )
        except Exception as exc:  # noqa: BLE001 -- transport failure is data
            return (
                _redact_text(f"{type(exc).__name__}: {exc}"),
                "",
                "invocation_error",
            )
        if not isinstance(output, dict):
            raw, parsed, state = _wrap_client_output(output)
            return (_redact_text(raw), _redact_text(parsed), state)
        raw = output.get("raw_output", "")
        parsed = output.get("parsed_text", raw)
        state = output.get("failure_state", "ok")
        if output.get("truncated") is True:
            state = "truncated"
        if not isinstance(raw, str) or not isinstance(parsed, str):
            return ("client returned non-string output", "", "parse_error")
        try:
            state = schema.FailureState.coerce(state).value
        except schema.ContractError:
            return (_redact_text(raw), _redact_text(parsed), "parse_error")
        if state == "ok" and not parsed.strip():
            state = "empty"
        resource = output.get("resource", {})
        if isinstance(resource, dict):
            self._last_client_resource = dict(resource)
            reported = resource.get("model_version")
            pinned = self.spec.config.get("model_version")
            if (
                isinstance(reported, str)
                and reported
                and isinstance(pinned, str)
                and pinned
                and reported != pinned
            ):
                self._last_version_mismatch = True
        return (_redact_text(raw), _redact_text(parsed), state)


class GoogleVisionAdapter(VendorOCRAdapter):
    """GV -- Google Vision DOCUMENT_TEXT_DETECTION vendor recognizer."""


class AzureReadAdapter(VendorOCRAdapter):
    """AZ -- Azure AI Vision Read vendor recognizer."""


ADAPTERS: dict[str, type[BaseAdapter]] = {
    "B0": UnchangedTesseractAdapter,
    "B1": UnavailableRecognizerAdapter,
    "B2": UnavailableRecognizerAdapter,
    "B3": TextOnlyCorrectionAdapter,
    "B4": ExistingPolicyAdapter,
    "B5": EvidenceGateAdapter,
    "GV": GoogleVisionAdapter,
    "AZ": AzureReadAdapter,
}


def make_adapter(spec: AdapterSpec) -> BaseAdapter:
    """Instantiate the registered adapter class for ``spec.adapter_id``."""
    try:
        cls = ADAPTERS[spec.adapter_id]
    except KeyError:
        raise AdapterUnavailable(
            f"no registered adapter for {spec.adapter_id!r}"
        ) from None
    return cls(spec)


def load_adapter_specs(path: str = "research/configs/baselines.json") -> list[AdapterSpec]:
    """Load frozen baseline specs; resolves ``path`` against the repo root."""
    candidate = Path(path)
    if not candidate.is_absolute() and not candidate.exists():
        repo_root = Path(__file__).resolve().parents[2]
        rooted = repo_root / path
        if rooted.exists():
            candidate = rooted
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise schema.ContractError(
            f"cannot load adapter specs from {candidate}: {exc}"
        ) from exc
    schema.assert_no_gold_fields(data, context="adapter specs")
    if not isinstance(data, dict) or not isinstance(data.get("adapters"), list):
        raise schema.ContractError(
            f"adapter specs file {candidate} must hold an 'adapters' list"
        )
    specs = []
    required = {
        "adapter_id",
        "kind",
        "model_id",
        "prompt_id",
        "prompt_hash",
        "crop_policy",
        "config",
    }
    for entry in data["adapters"]:
        if not isinstance(entry, dict):
            raise schema.ContractError("adapter spec entries must be dicts")
        unknown = set(entry) - required
        if unknown:
            raise schema.ContractError(
                f"unknown keys for AdapterSpec: {sorted(unknown)}"
            )
        missing = required - set(entry)
        if missing:
            raise schema.ContractError(
                f"AdapterSpec missing required keys: {sorted(missing)}"
            )
        specs.append(AdapterSpec(**{key: entry[key] for key in required}))
    return specs
