"""A Strands model that plays back scripted responses, so the LLM agent's MCP wiring,
hooks and guards can be tested against the real server without a model API."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from typing import Any

from strands.models import Model

Step = dict[str, Any] | Callable[[list], dict[str, Any]]


def tool_results(messages: list) -> list[tuple[str, dict]]:
    """(tool_use_id, parsed JSON) for every tool result in the conversation, oldest first."""
    out = []
    for m in messages:
        for block in m.get("content", []):
            tr = block.get("toolResult")
            if not tr:
                continue
            for c in tr.get("content", []):
                try:
                    out.append((tr["toolUseId"], json.loads(c.get("text", ""))))
                except (TypeError, ValueError):
                    continue
    return out


def last(messages: list, key: str) -> Any:
    """The most recent value of `key` in any tool result."""
    for _, data in reversed(tool_results(messages)):
        if isinstance(data, dict) and key in data:
            return data[key]
    raise KeyError(key)


def notice_from_context(messages: list) -> dict[str, str]:
    text = next(b["text"] for m in reversed(messages) if m["role"] == "user" for b in m["content"] if "text" in b)
    line = next(x for x in text.splitlines() if x.startswith("[School notice received"))
    source = line.split("source: ", 1)[1].split(";")[0]
    received = line.split("received_at: ", 1)[1].strip()
    raw = next(x for x in text.splitlines() if x.startswith("notice text: "))[len("notice text: "):]
    return {"raw_notice": raw, "source": source, "received_at": received}


class ScriptedModel(Model):
    def __init__(self, steps: list[Step] | None = None):
        self.steps: list[Step] = list(steps or [])
        self.config: dict[str, Any] = {"model_id": "scripted"}
        self.tool_names_seen: list[list[str]] = []
        self.calls = 0

    def script(self, *steps: Step) -> "ScriptedModel":
        self.steps.extend(steps)
        return self

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> Any:
        return self.config

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs) -> AsyncGenerator[Any, None]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs) -> AsyncGenerator[dict, None]:
        self.calls += 1
        self.tool_names_seen.append([t["name"] for t in tool_specs or []])
        if not self.steps:
            raise RuntimeError("scripted model ran out of steps")
        step = self.steps.pop(0)
        if callable(step):
            step = step(messages)
        if "raise" in step:
            raise step["raise"]
        yield {"messageStart": {"role": "assistant"}}
        if "tool" in step:
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"tu-{self.calls}", "name": step["tool"]}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(step.get("input", {}))}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockDelta": {"delta": {"text": step["text"]}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
