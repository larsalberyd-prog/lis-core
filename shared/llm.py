"""Anthropic SDK wrapper.

Pins the model, enables prompt caching on the system block, and emits cost-loggning
per call. Agents never instantiate `anthropic.Anthropic` directly — they call
`call_llm()` so we have one chokepoint for retry, cost tracking, and model upgrades.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"
ESCALATION_MODEL = "claude-opus-4-7"

# Sonnet 4.6 pricing per 1M tokens (USD). Cache reads are 10% of base input.
PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-opus-4-7": {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
}


@dataclass
class LLMUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        p = PRICING.get(self.model, PRICING[DEFAULT_MODEL])
        return (
            self.input_tokens * p["input"]
            + self.output_tokens * p["output"]
            + self.cache_read_tokens * p["cache_read"]
            + self.cache_creation_tokens * p["cache_write"]
        ) / 1_000_000


@dataclass
class LLMResult:
    text: str
    usage: LLMUsage
    raw: Any = None


@dataclass
class CostLogger:
    total: LLMUsage = field(default_factory=lambda: LLMUsage(model=DEFAULT_MODEL))
    per_agent: dict[str, LLMUsage] = field(default_factory=dict)

    def record(self, agent_name: str, usage: LLMUsage) -> None:
        self.total.input_tokens += usage.input_tokens
        self.total.output_tokens += usage.output_tokens
        self.total.cache_read_tokens += usage.cache_read_tokens
        self.total.cache_creation_tokens += usage.cache_creation_tokens
        bucket = self.per_agent.setdefault(agent_name, LLMUsage(model=usage.model))
        bucket.input_tokens += usage.input_tokens
        bucket.output_tokens += usage.output_tokens
        bucket.cache_read_tokens += usage.cache_read_tokens
        bucket.cache_creation_tokens += usage.cache_creation_tokens


COST_LOGGER = CostLogger()


def call_llm(
    *,
    agent_name: str,
    system: str,
    messages: list[dict[str, Any]],
    model: str = DEFAULT_MODEL,
    max_tokens: int = 2048,
    temperature: float = 0.0,
    cache_system: bool = True,
) -> LLMResult:
    """Single chokepoint for all LLM calls.

    System prompt is cache-marked by default — agent prompts are stable, account data
    in `messages` is what varies. That keeps cache-hit rate high across a nightly run.
    """
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("anthropic SDK required: pip install anthropic") from e

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic()

    system_blocks: list[dict[str, Any]]
    if cache_system:
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    else:
        system_blocks = [{"type": "text", "text": system}]

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_blocks,
        messages=messages,
    )

    u = response.usage
    usage = LLMUsage(
        model=model,
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_creation_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )
    COST_LOGGER.record(agent_name, usage)
    logger.info(
        "llm_call agent=%s model=%s in=%d out=%d cache_read=%d cache_write=%d cost=$%.4f",
        agent_name,
        model,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_creation_tokens,
        usage.cost_usd,
    )

    text = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text += block.text
    return LLMResult(text=text, usage=usage, raw=response)


def load_prompt(prompt_path: str) -> str:
    """Load an agent's prompt.md from disk. Keeps prompt text out of agent.py."""
    with open(prompt_path, encoding="utf-8") as f:
        return f.read()
