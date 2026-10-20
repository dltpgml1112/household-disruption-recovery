"""Model provider for the LLM agent. Pluggable: the agent only needs a Strands `Model`.

Gemini is the default because it has a free API tier. Any other Strands provider
(Bedrock, Anthropic, OpenAI, Ollama ...) can be added here without touching the agent.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from typing import Any

from strands.models import Model
from strands.types.exceptions import ModelThrottledException

# gemini-3.8-flash allows 20 requests a day on the free tier; one evaluation run needs
# about 230. Flash-Lite allows 500 a day, which is what this default is chosen for.
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_GEMINI_PARAMS = {"thinking_config": {"thinking_level": "LOW"}}


class DailyQuotaExhausted(Exception):
    """The model's per-day request quota is used up. Not a ModelThrottledException on purpose:
    retrying cannot succeed before the daily reset, so the turn should fail at once."""


def per_day_quota(details: Any) -> bool:
    """True if a Gemini error body names a per-day quota (QuotaFailure quotaId "...PerDay...")."""
    if isinstance(details, dict):
        quota_id = details.get("quotaId")
        if isinstance(quota_id, str) and "PerDay" in quota_id:
            return True
        return any(per_day_quota(v) for v in details.values())
    if isinstance(details, list):
        return any(per_day_quota(v) for v in details)
    return False


def _gemini_model_class():
    from google.genai import errors
    from strands.models.gemini import GeminiModel

    class RetryingGeminiModel(GeminiModel):
        """GeminiModel with retries that match what the free tier actually returns.

        Strands maps every Gemini 429 to ModelThrottledException, which the retry strategy
        backs off and retries. Two changes:
        - a 503 "high demand" (a ServerError, which would end the turn at once) is retried
          like a 429;
        - a 429 for a per-day quota is not retried, since it cannot succeed until the
          daily reset. Retrying it made each turn wait over two minutes before the
          fallback agent could answer.
        """

        async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
            try:
                async for event in super().stream(*args, **kwargs):
                    yield event
            except errors.ServerError as e:
                if e.code in (500, 503):
                    raise ModelThrottledException(e.message or str(e)) from e
                raise
            except ModelThrottledException as e:
                cause = e.__cause__
                if isinstance(cause, errors.APIError) and per_day_quota(cause.details):
                    raise DailyQuotaExhausted(str(e)) from e
                raise

    return RetryingGeminiModel


@dataclass
class ModelChoice:
    model: Model | None
    provider: str
    model_id: str | None
    reason: str | None = None  # why there is no model, if there isn't one


def model_from_env(env: Mapping[str, str]) -> ModelChoice:
    provider = (env.get("HDR_LLM_PROVIDER") or "gemini").lower()
    if provider == "gemini":
        params = json.loads(env["HDR_LLM_PARAMS"]) if env.get("HDR_LLM_PARAMS") else dict(DEFAULT_GEMINI_PARAMS)
        key = env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY")
        model_id = env.get("HDR_LLM_MODEL") or DEFAULT_GEMINI_MODEL
        if not key:
            return ModelChoice(None, provider, model_id, "GEMINI_API_KEY is not set")
        cls = _gemini_model_class()
        return ModelChoice(cls(client_args={"api_key": key}, model_id=model_id, params=params), provider, model_id)
    return ModelChoice(None, provider, None, f"unknown HDR_LLM_PROVIDER {provider!r}")
