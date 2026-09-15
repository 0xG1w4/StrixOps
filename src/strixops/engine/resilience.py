"""SDK-native retries and eligibility for session-backed stream recovery.

Context compaction and overflow recovery live in ``compaction`` and ``loop``;
they operate on the persisted agent session.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx
from agents.model_settings import ModelSettings
from agents.retry import ModelRetryBackoffSettings, ModelRetrySettings, RetryPolicyContext, retry_policies
from agents.tool import ApplyPatchTool, ComputerTool, FunctionTool, LocalShellTool, ShellTool
from openai import APIConnectionError, APIError, APIStatusError

from strixops.config.model_errors import upstream_policy_code

_TRANSIENT_STREAM_STATUSES = frozenset({429, 500, 502, 503, 504})
_TRANSIENT_STREAM_CODES = frozenset({"server_error", "internal_server_error", "rate_limit_exceeded"})
_PERMANENT_STREAM_CODES = frozenset(
    {
        "bad_request_error",
        "authentication_error",
        "authorization_error",
        "unauthorized",
        "forbidden",
        "permission_error",
        "permission_denied",
        "permission_denied_error",
        "not_found_error",
        "unprocessable_entity_error",
        "insufficient_quota",
        "billing_error",
        "content_filter",
        "context_length_exceeded",
    }
)


@dataclass
class ModelStreamFailure:
    """Per-request evidence, attached only by the model boundary, never a tool."""

    completed: bool = False
    replay_unsafe: bool = False
    request_attempt: int = 1
    provider_veto: bool = False


def model_stream_failure(error: BaseException) -> ModelStreamFailure | None:
    for candidate in _error_chain(error):
        failure = getattr(candidate, "_strixops_model_stream_failure", None)
        if isinstance(failure, ModelStreamFailure):
            return failure
    return None


def _error_chain(error: BaseException):
    seen: set[int] = set()
    for _ in range(16):
        if id(error) in seen:
            break
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__
        if error is None:
            break


def local_tools_only(tools: list) -> bool:
    """Hosted tools can have side effects before the interrupted response ends."""
    for tool in tools:
        if isinstance(tool, ShellTool):
            if (tool.environment or {}).get("type", "local") != "local":
                return False
        elif not isinstance(tool, (FunctionTool, LocalShellTool, ApplyPatchTool, ComputerTool)):
            return False
    return True


def can_resume_model_stream(error: Exception) -> bool:
    """Recover a stateless model request from the durable local session.

    The SDK already retries failures before output. Do not multiply its exhausted
    budget, retry tool/storage errors, or replay a remote conversation/hosted tool.
    """
    failure = model_stream_failure(error)
    if (
        failure is None
        or failure.completed
        or failure.replay_unsafe
        or failure.provider_veto
        or failure.request_attempt > (MODEL_RETRY.max_retries or 0)
        or upstream_policy_code(error) is not None
    ):
        return False
    transient = False
    for candidate in _error_chain(error):
        if candidate.__class__.__name__ in {"CancelledError", "AbortError"}:
            return False
        status = _stream_status(getattr(candidate, "status_code", None))
        if status is not None:
            if status not in _TRANSIENT_STREAM_STATUSES | {408}:
                return False
            transient = True
        if isinstance(candidate, APIError):
            pending = [candidate.body]
            seen: set[int] = set()
            for body in pending:
                if not isinstance(body, Mapping) or id(body) in seen:
                    continue
                seen.add(id(body))
                labels = {
                    value.lower()
                    for key in ("code", "type")
                    if isinstance(value := body.get(key), str) and _stream_status(value) is None
                }
                if labels & _PERMANENT_STREAM_CODES or any(
                    label.startswith(("invalid_", "unsupported_")) for label in labels
                ):
                    return False
                transient |= bool(labels & _TRANSIENT_STREAM_CODES)
                for key in ("status", "status_code", "code"):
                    value = _stream_status(body.get(key))
                    if value is not None:
                        if value not in _TRANSIENT_STREAM_STATUSES | {408}:
                            return False
                        transient = True
                if len(pending) < 64:
                    pending.extend(body.get(key) for key in ("error", "innererror", "inner_error"))
        if isinstance(
            candidate,
            (
                APIConnectionError,
                httpx.ConnectError,
                httpx.ReadError,
                httpx.WriteError,
                httpx.RemoteProtocolError,
                httpx.TimeoutException,
                TimeoutError,
                ConnectionResetError,
                ConnectionAbortedError,
                BrokenPipeError,
            ),
        ):
            transient = True
    return transient


def transport_recovery_delay(attempt: int) -> float:
    return min(2.0 * 2 ** (attempt - 1), 90.0)


def _stream_status(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 100 <= value <= 599 else None
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) == 3:
        return _stream_status(int(value))
    return None


def _explicit_transient_stream_error(context: RetryPolicyContext) -> bool:
    """Recognize structured SSE errors that have no HTTP error status.

    OpenAI's SSE parser raises plain APIError for a gateway's error envelope,
    even when its body contains an upstream 5xx code. The SDK still decides
    whether replay is safe: this predicate cannot retry after output/tool events
    or override stateful request protections. A generic error message is not
    evidence that retrying will help.
    """
    if not context.stream:
        return False
    error: BaseException | None = context.error
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, APIStatusError):
            # HTTP failures already have provider advice and status policies.
            return False
        if isinstance(error, APIError) and isinstance(error.body, Mapping):
            body = error.body
            details = [body]
            seen_details = {id(body)}
            for detail in details:
                for key in ("error", "innererror"):
                    nested = detail.get(key)
                    if isinstance(nested, Mapping) and id(nested) not in seen_details:
                        seen_details.add(id(nested))
                        details.append(nested)
            statuses = [
                status
                for detail in details
                for key in ("status_code", "status", "code")
                if (status := _stream_status(detail.get(key))) is not None
            ]
            labels = {
                value.lower()
                for detail in details
                for key in ("code", "type")
                if isinstance(value := detail.get(key), str) and _stream_status(value) is None
            }
            if any(status not in _TRANSIENT_STREAM_STATUSES for status in statuses):
                return False
            if labels & _PERMANENT_STREAM_CODES or any(
                label.startswith(("invalid_", "unsupported_")) for label in labels
            ):
                return False
            return bool(statuses or labels & _TRANSIENT_STREAM_CODES)
        error = error.__cause__ or error.__context__
    return False


def _not_explicit_policy_rejection(context: RetryPolicyContext) -> bool:
    # Gate every transient/provider suggestion, including a gateway's wrapped
    # code500, when its error explicitly identifies a policy rejection.
    return upstream_policy_code(context.error) is None


MODEL_RETRY = ModelRetrySettings(
    max_retries=5,
    backoff=ModelRetryBackoffSettings(initial_delay=2.0, max_delay=90.0, multiplier=2.0, jitter=False),
    policy=retry_policies.all(
        _not_explicit_policy_rejection,
        retry_policies.any(
            retry_policies.provider_suggested(),
            retry_policies.network_error(),
            retry_policies.http_status((429, 500, 502, 503, 504)),
            _explicit_transient_stream_error,
        ),
    ),
)


def model_settings() -> ModelSettings:
    """Model settings every agent run gets: transient retry, usage reporting,
    no parallel tool calls (degenerate multi-call turns are harder to manage)."""
    return ModelSettings(
        retry=MODEL_RETRY,
        include_usage=True,
        parallel_tool_calls=False,
    )
