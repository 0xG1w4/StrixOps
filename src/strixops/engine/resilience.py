"""SDK-native transient model retries.

Context compaction and overflow recovery live in ``compaction`` and ``loop``;
they operate on the persisted agent session.
"""

from __future__ import annotations

from agents.model_settings import ModelSettings
from agents.retry import ModelRetryBackoffSettings, ModelRetrySettings, retry_policies

MODEL_RETRY = ModelRetrySettings(
    max_retries=5,
    backoff=ModelRetryBackoffSettings(initial_delay=2.0, max_delay=90.0, multiplier=2.0, jitter=False),
    policy=retry_policies.any(
        retry_policies.provider_suggested(),
        retry_policies.network_error(),
        retry_policies.http_status((429, 500, 502, 503, 504)),
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
