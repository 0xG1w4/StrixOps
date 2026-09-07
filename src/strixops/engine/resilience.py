"""SDK-native transient model retries.

Context compaction and overflow recovery live in ``compaction`` and ``loop``;
they operate on the persisted agent session.
"""

from __future__ import annotations

from collections.abc import Mapping

from agents.model_settings import ModelSettings
from agents.retry import ModelRetryBackoffSettings, ModelRetrySettings, RetryPolicyContext, retry_policies
from openai import APIError, APIStatusError

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
