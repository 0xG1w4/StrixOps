"""Reference-compatible settings for conversation compaction and tool output."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ContextSettings(BaseSettings):
    """Keep the Strix context defaults and environment names unchanged."""

    model_config = SettingsConfigDict(case_sensitive=False, populate_by_name=True, extra="ignore")

    auto_compact: bool = Field(default=True, alias="STRIX_CONTEXT_AUTO_COMPACT")
    compact_buffer_tokens: int = Field(default=20_000, gt=0, alias="STRIX_CONTEXT_BUFFER_TOKENS")
    keep_tokens: int = Field(default=8_000, gt=0, alias="STRIX_CONTEXT_KEEP_TOKENS")
    fallback_context_tokens: int = Field(default=200_000, gt=0, alias="STRIX_CONTEXT_FALLBACK_TOKENS")
    summary_max_tokens: int = Field(default=4_096, gt=0, alias="STRIX_CONTEXT_SUMMARY_TOKENS")
    tool_output_max_tokens: int = Field(default=8_000, gt=0, alias="STRIX_TOOL_OUTPUT_MAX_TOKENS")
    tool_output_max_lines: int = Field(default=2_000, gt=0, alias="STRIX_TOOL_OUTPUT_MAX_LINES")
    tool_output_max_bytes: int = Field(default=50 * 1024, ge=1024, alias="STRIX_TOOL_OUTPUT_MAX_BYTES")
    max_context_images: int = Field(default=3, ge=0, alias="STRIX_MAX_CONTEXT_IMAGES")
    probe_enabled: bool = Field(default=True, alias="STRIX_CONTEXT_PROBE_ENABLED")
    probe_max_requests: int = Field(default=4, ge=1, le=16, alias="STRIX_CONTEXT_PROBE_MAX_REQUESTS")
    probe_timeout_seconds: float = Field(
        default=60, gt=0, le=600, alias="STRIX_CONTEXT_PROBE_TIMEOUT_SECONDS",
    )
    probe_request_timeout_seconds: float = Field(
        default=20, gt=0, le=120, alias="STRIX_CONTEXT_PROBE_REQUEST_TIMEOUT_SECONDS",
    )
    probe_max_input_tokens: int = Field(
        default=32_768, ge=512, le=1_048_576, alias="STRIX_CONTEXT_PROBE_MAX_INPUT_TOKENS",
    )
    probe_total_input_tokens: int = Field(
        default=65_536, ge=512, le=4_194_304, alias="STRIX_CONTEXT_PROBE_TOTAL_INPUT_TOKENS",
    )
