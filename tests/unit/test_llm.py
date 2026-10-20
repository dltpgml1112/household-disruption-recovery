import anyio
import pytest
from google.genai import errors
from strands.models.gemini import GeminiModel
from strands.types.exceptions import ModelThrottledException

from hdr.orchestrator.llm import DailyQuotaExhausted, model_from_env, per_day_quota


def _overloaded(code: int):
    async def stream(self, *args, **kwargs):
        raise errors.ServerError(code, {"error": {"code": code, "message": "high demand", "status": "UNAVAILABLE"}})
        yield  # pragma: no cover

    return stream


def _quota(quota_id: str):
    """What Strands' GeminiModel raises for a 429: ModelThrottledException from the ClientError."""

    async def stream(self, *args, **kwargs):
        body = {"error": {"code": 429, "message": "You exceeded your current quota", "status": "RESOURCE_EXHAUSTED",
                          "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                       "violations": [{"quotaId": quota_id, "quotaValue": "20"}]}]}}
        error = errors.ClientError(429, body)
        raise ModelThrottledException(error.message) from error
        yield  # pragma: no cover

    return stream


def _drain(model):
    async def run():
        async for _ in model.stream([{"role": "user", "content": [{"text": "hi"}]}]):
            pass

    anyio.run(run)


def test_no_key_means_no_model():
    choice = model_from_env({})
    assert choice.model is None and choice.reason == "GEMINI_API_KEY is not set"


def test_overload_is_retryable(monkeypatch):
    monkeypatch.setattr(GeminiModel, "stream", _overloaded(503))
    model = model_from_env({"GEMINI_API_KEY": "test-key-not-real", "HDR_LLM_MODEL": "m"}).model
    with pytest.raises(ModelThrottledException):
        _drain(model)


def test_other_server_errors_still_fail(monkeypatch):
    monkeypatch.setattr(GeminiModel, "stream", _overloaded(501))
    model = model_from_env({"GEMINI_API_KEY": "test-key-not-real", "HDR_LLM_MODEL": "m"}).model
    with pytest.raises(errors.ServerError):
        _drain(model)


def test_defaults_and_overrides():
    choice = model_from_env({"GEMINI_API_KEY": "test-key-not-real"})
    assert choice.model_id == "gemini-3.5-flash-lite"
    assert choice.model.get_config()["params"] == {"thinking_config": {"thinking_level": "LOW"}}
    choice = model_from_env({"GEMINI_API_KEY": "test-key-not-real", "HDR_LLM_MODEL": "gemini-3.8-flash",
                             "HDR_LLM_PARAMS": '{"temperature": 0}'})
    assert choice.model_id == "gemini-3.8-flash"
    assert choice.model.get_config()["params"] == {"temperature": 0}


def test_per_day_quota_is_not_retried(monkeypatch):
    monkeypatch.setattr(GeminiModel, "stream", _quota("GenerateRequestsPerDayPerProjectPerModel-FreeTier"))
    model = model_from_env({"GEMINI_API_KEY": "test-key-not-real", "HDR_LLM_MODEL": "m"}).model
    with pytest.raises(DailyQuotaExhausted):
        _drain(model)
    assert not issubclass(DailyQuotaExhausted, ModelThrottledException)  # the retry strategy skips it


def test_per_minute_quota_is_still_retried(monkeypatch):
    monkeypatch.setattr(GeminiModel, "stream", _quota("GenerateRequestsPerMinutePerProjectPerModel-FreeTier"))
    model = model_from_env({"GEMINI_API_KEY": "test-key-not-real", "HDR_LLM_MODEL": "m"}).model
    with pytest.raises(ModelThrottledException):
        _drain(model)


def test_per_day_quota_detection():
    assert per_day_quota([{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}])
    assert not per_day_quota({"error": {"details": [{"violations": [{"quotaId": "PerMinute"}]}]}})
    assert not per_day_quota(None)
