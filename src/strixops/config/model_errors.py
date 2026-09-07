"""Bounded model failure diagnostics without dumping provider payloads or requests."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from openai import APIError

_TOKEN = re.compile(r"[A-Za-z0-9_.:/\[\]-]{1,160}\Z")
UPSTREAM_CYBER_POLICY_MESSAGE = (
    "The model provider blocked this request under its cybersecurity policy (cyber_policy). "
    "Review the provider's access requirements for authorized cybersecurity work."
)


def _embedded_error_objects(value: str) -> list[object]:
    """Parse bounded JSON error objects inside Azure/LiteLLM error messages."""
    text = value[:65536]
    decoder = json.JSONDecoder()
    objects = []
    cursor = 0
    for _ in range(32):
        start = text.find("{", cursor)
        if start < 0:
            break
        try:
            obj, cursor = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            cursor = start + 1
        else:
            objects.append(obj)
    return objects


def upstream_policy_code(exc: BaseException) -> str | None:
    """Recognize an explicit provider error code; generic 500s prove no policy.

    Azure's structured error can be JSON embedded in LiteLLM's message string.
    Follow only error-envelope fields, never arbitrary prompt/tool payloads or
    a loose mention of policy text. Keep traversal bounded and cycle-safe.
    """
    error: BaseException | None = exc
    seen_errors: set[int] = set()
    for _ in range(16):
        if error is None or id(error) in seen_errors:
            break
        seen_errors.add(id(error))
        if isinstance(error, APIError):
            pending: list[object] = [error.body, str(error)]
            seen_objects: set[int] = set()
            for index in range(128):
                if index >= len(pending):
                    break
                value = pending[index]
                if id(value) in seen_objects:
                    continue
                seen_objects.add(id(value))
                if isinstance(value, Mapping):
                    code = value.get("code")
                    if isinstance(code, str) and code.lower() == "cyber_policy":
                        return "cyber_policy"
                    children = [value.get(key) for key in ("error", "innererror", "inner_error", "message")]
                    pending.extend(children[: 128 - len(pending)])
                elif isinstance(value, str):
                    pending.extend(_embedded_error_objects(value)[: 128 - len(pending)])
        error = error.__cause__ or error.__context__
    return None


def _request_secrets(exc: APIError) -> set[str]:
    secrets = set()
    for name in ("authorization", "api-key", "x-api-key"):
        value = exc.request.headers.get(name, "")
        if value:
            secrets.add(value)
            if name == "authorization" and " " in value:
                secrets.add(value.split(" ", 1)[1])
    return secrets


def model_error_details(exc: APIError) -> str:
    """Expose only diagnostic identifiers, never messages, inputs, URLs or headers.

    Stream errors often have no HTTP failure status: the connection returned 200
    before the provider emitted an error. Its structured body may still identify
    the underlying failure, even when the top-level message is generic.
    """
    secrets = _request_secrets(exc)
    fields: dict[str, object] = {}
    path = exc.request.url.path.rstrip("/")
    if path.endswith("/responses"):
        fields["api"] = "responses"
    elif path.endswith("/chat/completions"):
        fields["api"] = "chat_completions"
    fields.update({
        "status": getattr(exc, "status_code", None),
        "code": exc.code,
        "type": exc.type,
        "param": exc.param,
        "request_id": getattr(exc, "request_id", None),
        "gateway_request_id": getattr(exc, "_strixops_gateway_request_id", None),
        "model_request_attempt": getattr(exc, "_strixops_model_request_attempt", None),
        "stream_events": getattr(exc, "_strixops_stream_event_count", None),
        "retry_blocked_by": getattr(exc, "_strixops_stream_retry_blocked_by", None),
        "upstream_policy": upstream_policy_code(exc),
    })
    body = exc.body
    for _ in range(3):
        if not isinstance(body, Mapping):
            break
        for name in ("code", "type", "param", "request_id"):
            if not fields.get(name):
                fields[name] = body.get(name)
        if not fields.get("status"):
            fields["status"] = body.get("status_code") or body.get("status")
        body = body.get("error") or body.get("innererror")
    details = []
    for name, value in fields.items():
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            continue
        token = str(value)
        if not _TOKEN.fullmatch(token) or any(secret in token for secret in secrets):
            continue
        if token.lower().startswith(("sk-", "sk_")):
            continue
        details.append(f"{name}={token}")
    return "; ".join(details)


def format_model_error(exc: Exception) -> str:
    policy = upstream_policy_code(exc)
    if not isinstance(exc, APIError):
        if policy:
            return f"{type(exc).__name__}: {UPSTREAM_CYBER_POLICY_MESSAGE}"
        return f"{type(exc).__name__}: {exc}"
    message = UPSTREAM_CYBER_POLICY_MESSAGE if policy else str(exc)
    for secret in sorted(_request_secrets(exc), key=len, reverse=True):
        message = message.replace(secret, "[redacted]")
    message = re.sub(r"(?i)Bearer\s+\S+|\bsk-[A-Za-z0-9_-]+", "[redacted]", message)
    message = " ".join(message.split())
    if len(message) > 900:
        message = message[:900] + "…"
    details = model_error_details(exc)
    return f"{type(exc).__name__}: {message}" + (f" [{details}]" if details else "")
