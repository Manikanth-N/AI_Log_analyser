"""
Anthropic inference provider.

Handles Claude Sonnet, Haiku, Opus via the Anthropic API.
Uses instructor for structured output with Pydantic schema validation.

Key API differences from OpenAI:
  - system prompt is a separate string parameter (not a "system" role message)
  - max_tokens is required (not optional)
  - usage: input_tokens / output_tokens (not prompt_tokens / completion_tokens)
"""

from __future__ import annotations

from typing import TypeVar

import instructor
import structlog
from pydantic import BaseModel

from .base import InferenceProvider, TokenUsage

log = structlog.get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

# Sentinel so we can detect "no api key configured" at startup
_NO_KEY = "__no_anthropic_key__"


class AnthropicProvider:
    """
    Anthropic API inference provider.

    Requires: pip install anthropic instructor
    """

    def __init__(self, api_key: str, timeout: float = 120.0):
        if not api_key or api_key == _NO_KEY:
            raise ValueError(
                "AnthropicProvider: ANTHROPIC_API_KEY is not set. "
                "Set the environment variable or configure anthropic_api_key in settings."
            )
        # Import here so the package is optional — fails fast with a clear message
        try:
            from anthropic import Anthropic, APIError, APITimeoutError, RateLimitError
        except ImportError as exc:
            raise ImportError(
                "anthropic package is required for AnthropicProvider. "
                "Run: pip install anthropic"
            ) from exc

        self._Anthropic = Anthropic
        self._APIError = APIError
        self._APITimeoutError = APITimeoutError
        self._RateLimitError = RateLimitError

        self._client = Anthropic(api_key=api_key, timeout=timeout)
        self._instructor = instructor.from_anthropic(self._client)
        self._last_usage = TokenUsage()

    @property
    def provider_id(self) -> str:
        return "anthropic"

    @property
    def default_model(self) -> str:
        return "claude-sonnet-4-6"

    def structured(
        self,
        messages: list[dict],
        response_model: type[T],
        model: str,
        system: str | None = None,
        temperature: float = 0.1,
        max_retries: int = 3,
    ) -> T:
        try:
            response, completion = self._instructor.messages.create_with_completion(
                model=model,
                max_tokens=8192,
                system=system or "",
                messages=messages,
                response_model=response_model,
                temperature=temperature,
                max_retries=max_retries,
            )
            if completion.usage:
                self._last_usage = TokenUsage(
                    input_tokens=completion.usage.input_tokens or 0,
                    output_tokens=completion.usage.output_tokens or 0,
                )
            log.debug(
                "anthropic_structured_ok",
                model=model,
                schema=response_model.__name__,
                input_tokens=self._last_usage.input_tokens,
                output_tokens=self._last_usage.output_tokens,
            )
            return response
        except (self._RateLimitError, self._APITimeoutError, self._APIError) as e:
            log.error("anthropic_structured_error", model=model,
                      schema=response_model.__name__, error=str(e))
            raise

    def complete(
        self,
        messages: list[dict],
        model: str,
        system: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> str:
        try:
            resp = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system or "",
                messages=messages,
                temperature=temperature,
            )
            if resp.usage:
                self._last_usage = TokenUsage(
                    input_tokens=resp.usage.input_tokens or 0,
                    output_tokens=resp.usage.output_tokens or 0,
                )
            return resp.content[0].text if resp.content else ""
        except (self._RateLimitError, self._APITimeoutError, self._APIError) as e:
            log.error("anthropic_complete_error", model=model, error=str(e))
            raise

    def last_usage(self) -> TokenUsage:
        return self._last_usage
