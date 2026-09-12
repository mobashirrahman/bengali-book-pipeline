"""Pay-per-call vendor OCR transports (Google Vision + Azure Read).

Each factory returns a client callable with the signature the vendor
adapters use::

    client(image_bytes=..., page_id=..., adapter_id=..., config=...)

returning ``{"raw_output", "parsed_text", "failure_state", "resource"}``.
Stdlib only (``urllib.request``/``urllib.error``, ``json``, ``os``, ``time``,
plus ``base64`` for the Google JSON payload). Keys and endpoints are read
from ``env`` at CALL time, never at import or construction time. Every error
string passes through :func:`_redact`, which strips secret values and
endpoint hosts; nothing is printed or logged here.

``rights_gate`` is a REQUIRED callable ``(page_id) -> bool``. When it
returns False the client yields ``unsupported`` without any network call.
There is intentionally no default that allows everything.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

GOOGLE_KEY_ENV = "GOOGLE_VISION_API_KEY"
AZURE_KEY_ENV = "AZURE_VISION_KEY"
AZURE_ENDPOINT_ENV = "AZURE_VISION_ENDPOINT"
GOOGLE_ANNOTATE_URL = "https://vision.googleapis.com/v1/images:annotate"


def _redact(text: str, secrets: list[str]) -> str:
    """Strip every secret value (and URL hosts within) from ``text``."""
    redacted = text if isinstance(text, str) else str(text)
    for secret in secrets:
        if not secret:
            continue
        redacted = redacted.replace(secret, "[redacted]")
        host = _host_of(secret)
        if host:
            redacted = redacted.replace(host, "[redacted-host]")
    return redacted


def _host_of(value: str) -> str:
    text = value.strip()
    lowered = text.lower()
    for scheme in ("https://", "http://"):
        if lowered.startswith(scheme):
            rest = text[len(scheme):]
            return rest.split("/", 1)[0].split("@")[-1].split(":", 1)[0]
    return ""


def _compact_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _unsupported_rights() -> dict:
    return {
        "raw_output": "rights review not recorded for page",
        "parsed_text": "",
        "failure_state": "unsupported",
        # Zero attempts: refused before any network call, so runners must
        # neither cache nor ledger this row.
        "resource": {"attempts": 0},
    }


def _require_gate(rights_gate, page_id: str):
    if not callable(rights_gate):
        raise TypeError("rights_gate must be a callable (page_id) -> bool")


def _read_body(response) -> bytes:
    reader = getattr(response, "read", None)
    if not callable(reader):
        return b""
    try:
        return reader() or b""
    except Exception:
        return b""


def _status_of(response, default: int = 200) -> int:
    status = getattr(response, "status", default)
    try:
        return int(status)
    except (TypeError, ValueError):
        return default


def make_google_vision_transport(
    *,
    rights_gate,
    env=os.environ,
    urlopen=urllib.request.urlopen,
    timeout: float = 60,
):
    """Build a Google Vision DOCUMENT_TEXT_DETECTION client transport."""
    _require_gate(rights_gate, "")

    def client(*, image_bytes: bytes, page_id: str, adapter_id: str,
               config: dict) -> dict:
        if not rights_gate(page_id):
            return _unsupported_rights()
        key = env.get(GOOGLE_KEY_ENV, "") if env is not None else ""
        secrets = [key] if key else []
        if not key:
            return {
                "raw_output": f"missing env {GOOGLE_KEY_ENV}",
                "parsed_text": "",
                "failure_state": "unsupported",
                "resource": {"attempts": 0},
            }
        config = dict(config or {})
        model = config.get("model_version", "builtin/stable")
        language_hints = config.get("language_hints", ["bn"])
        try:
            content = base64.b64encode(bytes(image_bytes)).decode("ascii")
        except (TypeError, ValueError) as exc:
            return {
                "raw_output": _redact(
                    f"invalid image bytes: {type(exc).__name__}: {exc}",
                    secrets,
                ),
                "parsed_text": "",
                "failure_state": "invocation_error",
                "resource": {},
            }
        payload = {
            "requests": [
                {
                    "image": {"content": content},
                    "features": [
                        {"type": "DOCUMENT_TEXT_DETECTION", "model": model}
                    ],
                    "imageContext": {"languageHints": list(language_hints)},
                }
            ]
        }
        body = _compact_json(payload).encode("utf-8")
        request = urllib.request.Request(
            GOOGLE_ANNOTATE_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,  # never in the URL
            },
            method="POST",
        )
        try:
            response = urlopen(request, timeout=timeout)
            status = _status_of(response)
            raw = _read_body(response)
        except urllib.error.HTTPError as exc:
            raw = _read_body(exc)
            message = _redact(
                f"google vision HTTP {exc.code}: {_body_message(raw)}",
                secrets,
            )
            return {
                "raw_output": message,
                "parsed_text": "",
                "failure_state": "invocation_error",
                "resource": {"http_status": int(exc.code), "attempts": 1},
            }
        except Exception as exc:  # noqa: BLE001 -- transport failure is data
            return {
                "raw_output": _redact(
                    f"{type(exc).__name__}: {exc}", secrets
                ),
                "parsed_text": "",
                "failure_state": "invocation_error",
                "resource": {},
            }
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError) as exc:
            return {
                "raw_output": _redact(
                    f"google vision non-JSON response "
                    f"HTTP {status}: {type(exc).__name__}",
                    secrets,
                ),
                "parsed_text": "",
                "failure_state": "invocation_error",
                "resource": {"http_status": status, "attempts": 1},
            }
        responses = data.get("responses", []) if isinstance(data, dict) else []
        first = responses[0] if responses else {}
        if isinstance(first, dict) and first.get("error"):
            return {
                "raw_output": _redact(
                    f"google vision error HTTP {status}: "
                    f"{_compact_json(first['error'])}",
                    secrets,
                ),
                "parsed_text": "",
                "failure_state": "invocation_error",
                "resource": {"http_status": status, "attempts": 1},
            }
        text = ""
        if isinstance(first, dict):
            annotation = first.get("fullTextAnnotation", {})
            if isinstance(annotation, dict):
                text = annotation.get("text", "") or ""
        raw_output = _redact(_compact_json(data), secrets)
        if not text.strip():
            return {
                "raw_output": raw_output,
                "parsed_text": "",
                "failure_state": "empty",
                "resource": {"http_status": status, "attempts": 1},
            }
        return {
            "raw_output": raw_output,
            "parsed_text": _redact(text, secrets),
            "failure_state": "ok",
            "resource": {"http_status": status, "attempts": 1},
        }

    return client


def _body_message(raw: bytes) -> str:
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return (raw[:200].decode("utf-8", "replace") if raw else "").strip()
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message", _compact_json(error)) or "")
        return _compact_json(data)[:500]
    return ""


def make_azure_read_transport(
    *,
    rights_gate,
    env=os.environ,
    urlopen=urllib.request.urlopen,
    sleeper=time.sleep,
    max_retries: int = 3,
    timeout: float = 60,
):
    """Build an Azure AI Vision Read client transport with bounded retries."""
    _require_gate(rights_gate, "")

    def client(*, image_bytes: bytes, page_id: str, adapter_id: str,
               config: dict) -> dict:
        if not rights_gate(page_id):
            return _unsupported_rights()
        key = env.get(AZURE_KEY_ENV, "") if env is not None else ""
        endpoint = (
            env.get(AZURE_ENDPOINT_ENV, "") if env is not None else ""
        )
        secrets = [value for value in (key, endpoint) if value]
        if not key:
            return {
                "raw_output": f"missing env {AZURE_KEY_ENV}",
                "parsed_text": "",
                "failure_state": "unsupported",
                "resource": {"attempts": 0},
            }
        if not endpoint:
            return {
                "raw_output": f"missing env {AZURE_ENDPOINT_ENV}",
                "parsed_text": "",
                "failure_state": "unsupported",
                "resource": {"attempts": 0},
            }
        config = dict(config or {})
        api_version = config.get("api_version", "2024-02-01")
        url = (
            f"{endpoint.rstrip('/')}/computervision/imageanalysis:analyze"
            f"?api-version={api_version}&features=read"
        )
        attempts = 0
        max_attempts = max(1, int(max_retries) + 1)
        last_error = "unknown error"
        last_status = 0
        while attempts < max_attempts:
            attempts += 1
            request = urllib.request.Request(
                url,
                data=bytes(image_bytes),
                headers={
                    "Ocp-Apim-Subscription-Key": key,
                    "Content-Type": "application/octet-stream",
                },
                method="POST",
            )
            try:
                response = urlopen(request, timeout=timeout)
                status = _status_of(response)
                raw = _read_body(response)
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                raw = _read_body(exc)
                retry_after = _retry_after_seconds(exc)
                if (
                    status == 429 or 500 <= status <= 599
                ) and attempts < max_attempts:
                    sleeper(min(retry_after, 60.0))
                    last_status = status
                    last_error = (
                        f"azure read HTTP {status}: {_body_message(raw)}"
                    )
                    continue
                return {
                    "raw_output": _redact(
                        f"azure read HTTP {status}: {_body_message(raw)}",
                        secrets,
                    ),
                    "parsed_text": "",
                    "failure_state": "invocation_error",
                    "resource": {
                        "http_status": status,
                        "attempts": attempts,
                    },
                }
            except Exception as exc:  # noqa: BLE001 -- failure is data
                return {
                    "raw_output": _redact(
                        f"{type(exc).__name__}: {exc}", secrets
                    ),
                    "parsed_text": "",
                    "failure_state": "invocation_error",
                    "resource": {"attempts": attempts},
                }
            if status == 429 or 500 <= status <= 599:
                retry_after = _retry_after_response(response)
                if attempts < max_attempts:
                    sleeper(min(retry_after, 60.0))
                    last_status = status
                    last_error = (
                        f"azure read HTTP {status}: {_body_message(raw)}"
                    )
                    continue
                return {
                    "raw_output": _redact(
                        f"azure read HTTP {status}: {_body_message(raw)}",
                        secrets,
                    ),
                    "parsed_text": "",
                    "failure_state": "invocation_error",
                    "resource": {
                        "http_status": status,
                        "attempts": attempts,
                    },
                }
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError) as exc:
                return {
                    "raw_output": _redact(
                        f"azure read non-JSON response HTTP {status}: "
                        f"{type(exc).__name__}",
                        secrets,
                    ),
                    "parsed_text": "",
                    "failure_state": "invocation_error",
                    "resource": {
                        "http_status": status,
                        "attempts": attempts,
                    },
                }
            return _azure_success(data, status, attempts, secrets)
        return {
            "raw_output": _redact(
                f"azure read retries exhausted: {last_error}", secrets
            ),
            "parsed_text": "",
            "failure_state": "invocation_error",
            "resource": {"http_status": last_status, "attempts": attempts},
        }

    return client


def _retry_after_seconds(exc) -> float:
    headers = getattr(exc, "headers", None) or {}
    getter = getattr(headers, "get", None)
    try:
        value = getter("Retry-After") if callable(getter) else None
    except Exception:
        value = None
    return _parse_retry_after(value)


def _retry_after_response(response) -> float:
    headers = getattr(response, "headers", None) or {}
    getter = getattr(headers, "get", None)
    try:
        value = getter("Retry-After") if callable(getter) else None
    except Exception:
        value = None
    return _parse_retry_after(value)


def _parse_retry_after(value) -> float:
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return 1.0
    if delay < 0:
        return 0.0
    return delay


def _azure_success(data, status: int, attempts: int,
                   secrets: list[str]) -> dict:
    model_version = ""
    lines: list[str] = []
    if isinstance(data, dict):
        reported = data.get("modelVersion", "")
        if isinstance(reported, str):
            model_version = reported
        read_result = data.get("readResult", {})
        blocks = (
            read_result.get("blocks", [])
            if isinstance(read_result, dict) else []
        )
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            for line in block.get("lines", []):
                if isinstance(line, dict) and isinstance(
                    line.get("text"), str
                ):
                    lines.append(line["text"])
    raw_output = _redact(_compact_json(data), secrets)
    text = "\n".join(lines)
    resource: dict = {"http_status": status, "attempts": attempts}
    if model_version:
        # String provenance for the adapter (which folds it into
        # version_mismatch and drops it before building the numeric-only
        # Prediction resource).
        resource["model_version"] = model_version
    if text.strip():
        return {
            "raw_output": raw_output,
            "parsed_text": _redact(text, secrets),
            "failure_state": "ok",
            "resource": resource,
        }
    return {
        "raw_output": raw_output,
        "parsed_text": "",
        "failure_state": "empty",
        "resource": resource,
    }
