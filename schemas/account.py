from typing import Literal

from pydantic import BaseModel, Field

from .signal import SignalAtCapture

ExpectedFrequency = Literal["high", "medium", "low", "unknown"]


class DecisionMaker(BaseModel):
    name: str
    role: str | None = None


class RelationshipBlocker(BaseModel):
    role: str
    history: str | None = None
    attitude: str | None = None


class Account(BaseModel):
    """Account schema — superset of prospects_seed.yaml + ICP.md anchor records.

    Fields fall in three groups:
      1. Identity & geography (name, country, geo, segment, district, salesperson)
      2. Expert / sales-curated input (rationale, decision makers, competitor incumbent)
      3. Captured signals (SignalAtCapture — promoted to full Signal with evidence later)
    """

    name: str
    aliases: list[str] = Field(default_factory=list)
    country: Literal["SE", "NO", "DK", "FI"] = "SE"
    geo: str | None = None
    segment: str | None = None

    district: str | None = None
    salesperson: str | None = None

    sources: list[str] = Field(default_factory=list)

    salesperson_rationale: str | None = None
    known_decision_makers: list[DecisionMaker] = Field(default_factory=list)

    expected_purchase_frequency: ExpectedFrequency = "unknown"

    competitor_incumbent: str | None = None
    displacement_window_signal: str | None = None
    suggested_entry_angle: str | None = None

    management_priority: bool = False
    coordination_flags: list[str] = Field(default_factory=list)

    signals_at_capture: list[SignalAtCapture] = Field(default_factory=list)

    disqualification_notes: str | None = None
    personal_relationship_blockers: list[RelationshipBlocker] = Field(default_factory=list)

    notes_for_eval: str | None = None
