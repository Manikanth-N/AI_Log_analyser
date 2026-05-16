"""
Anthropic inference provider.

Handles Claude Sonnet, Haiku, Opus via the Anthropic API.
Uses instructor for structured output with Pydantic schema validation.

Key API differences from OpenAI:
  - system prompt is a separate string parameter (not a "system" role message)
  - max_tokens is required (not optional)
  - usage: input_tokens / output_tokens (not prompt_tokens / completion_tokens)

Haiku 4.5 compatibility note:
  instructor's generate_anthropic_schema() passes model.model_json_schema() verbatim
  as the tool input_schema.  Pydantic emits $defs/$ref when the same nested model is
  referenced more than once (e.g. HypothesisRecord appears in both root_causes and
  refuted_hypotheses).  Sonnet 4.6 silently resolves $ref; Haiku 4.5 strictly rejects
  it with HTTP 400.  We patch generate_anthropic_schema at import time to inline all
  $ref/$defs before the schema reaches the API.
"""

from __future__ import annotations

import copy
import functools
from typing import TypeVar

import instructor
import structlog
from pydantic import BaseModel

from .base import InferenceProvider, TokenUsage

log = structlog.get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

_NO_KEY = "__no_anthropic_key__"


# ── $ref inliner ──────────────────────────────────────────────────────────────

def _inline_refs(schema: dict) -> dict:
    """
    Recursively resolve all $ref references in a JSON schema and strip $defs.

    Anthropic's tool input_schema validator (especially Haiku-class models)
    rejects schemas containing $ref / $defs.  This transforms the Pydantic
    output into a fully self-contained, flat schema.

    Handles: arbitrary nesting, multiple references to the same definition.
    Does NOT handle circular schemas (not produced by the Pydantic models in
    this project, but would infinite-loop if present).
    """
    defs = schema.get("$defs", {})
    if not defs:
        return schema

    def _resolve(node: object, defs: dict) -> object:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_name = node["$ref"].split("/")[-1]
                return _resolve(copy.deepcopy(defs[ref_name]), defs)
            return {k: _resolve(v, defs) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [_resolve(item, defs) for item in node]
        return node

    return _resolve(copy.deepcopy(schema), defs)  # type: ignore[return-value]


# ── Patch instructor's Anthropic schema generator ─────────────────────────────
# Must happen before any call to from_anthropic() or generate_anthropic_schema().

try:
    import instructor.processing.schema as _schema_mod

    _orig_openai_schema = _schema_mod.generate_openai_schema.__wrapped__  # unwrap lru_cache

    @functools.lru_cache(maxsize=256)
    def _generate_anthropic_schema_inlined(model: type[BaseModel]) -> dict:
        openai = _orig_openai_schema(model)
        inlined = _inline_refs(model.model_json_schema())
        return {
            "name": openai["name"],
            "description": openai["description"],
            "input_schema": inlined,
        }

    _schema_mod.generate_anthropic_schema = _generate_anthropic_schema_inlined
    # Clear any previously cached (wrong) entries
    if hasattr(_schema_mod.generate_anthropic_schema, "cache_clear"):
        _schema_mod.generate_anthropic_schema.cache_clear()

    log.debug("anthropic_schema_patch_applied", detail="$ref inlining active for Haiku compatibility")

except Exception as _patch_err:
    log.warning("anthropic_schema_patch_failed", error=str(_patch_err),
                detail="Haiku 4.5 may reject schemas with $ref; upgrade instructor if issues persist")


# ── Provider class ─────────────────────────────────────────────────────────────

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
        max_tokens: int = 8192,
    ) -> T:
        try:
            response, completion = self._instructor.messages.create_with_completion(
                model=model,
                max_tokens=max_tokens,
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
            log.error(
                "anthropic_structured_error",
                model=model,
                schema=response_model.__name__,
                error=str(e),
                status_code=getattr(e, "status_code", None),
                error_body=str(getattr(e, "body", None)),
            )
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
            log.error(
                "anthropic_complete_error",
                model=model,
                error=str(e),
                status_code=getattr(e, "status_code", None),
                error_body=str(getattr(e, "body", None)),
            )
            raise

    def last_usage(self) -> TokenUsage:
        return self._last_usage
