"""Signals agent I/O contract.

The classifier takes a *raw text event* (job ad headline+body, press
release, news snippet, etc.) and the *source context* (which integration
produced it, the URL, the publish date) and emits zero or more typed
`Signal`s with evidence.

Multiple signals per event are allowed: a job ad that hires a Production
Manager AND mentions a 50 MSEK plant expansion is two signals from one
source quote.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

from schemas.evidence import SourceType
from schemas.signal import IndustrialSignalType, Signal


class RawEvent(BaseModel):
    """A normalised event from any integration before signal classification."""

    source_type: SourceType
    source_url: HttpUrl | None = None
    text: str = Field(min_length=1)
    employer_name: str | None = None
    employer_orgnr: str | None = None
    workplace_city: str | None = None
    occupation: str | None = None
    published_at: datetime | None = None

    # When an LLM eventually fills the narrative, we keep the original raw
    # payload around for debugging without polluting the contract.
    extra: dict[str, str] = Field(default_factory=dict)


class SignalsAgentInput(BaseModel):
    events: list[RawEvent]
    account_name_hint: str | None = None
    # When True, ambiguous events escalate to LLM. False = deterministic only
    # (CI-friendly, no ANTHROPIC_API_KEY required).
    use_llm: bool = False


Confidence = Literal["high", "medium", "low"]


class ClassifiedSignal(BaseModel):
    signal: Signal
    matched_keywords: list[str] = Field(default_factory=list)
    confidence: Confidence = "medium"
    # Name of the employer as it appeared in the event — discovery agent uses
    # this to attach the Signal to the right Account.
    employer_name: str | None = None


class SignalsAgentOutput(BaseModel):
    classified: list[ClassifiedSignal] = Field(default_factory=list)
    skipped_event_count: int = 0
