"""Responses transport retains SDK-native function outputs, including images."""

from agents.models.openai_responses import OpenAIResponsesModel

from strixops.config.reasoning_transport import RouteReasoningMixin


class PlatformResponsesModel(RouteReasoningMixin, OpenAIResponsesModel):
    """Responses model with route reasoning shared by agents and helper calls."""
