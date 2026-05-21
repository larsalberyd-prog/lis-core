from typing import Literal

from pydantic import BaseModel, Field

from .account import Account
from .signal import Signal

Tier = Literal["AAA", "AA", "A", "B", "C"]

TIER_ORDER: list[Tier] = ["C", "B", "A", "AA", "AAA"]


def tier_from_total(total: int) -> Tier:
    """SCORING.md §1 thresholds."""
    if total >= 80:
        return "AAA"
    if total >= 65:
        return "AA"
    if total >= 50:
        return "A"
    if total >= 35:
        return "B"
    return "C"


def tier_distance(a: Tier, b: Tier) -> int:
    return abs(TIER_ORDER.index(a) - TIER_ORDER.index(b))


class ScoreBreakdown(BaseModel):
    """SCORING.md §1 dimensions. Each is capped at the dimension max."""

    firmographic: int = Field(ge=0, le=30, default=0)
    capacity: int = Field(ge=0, le=20, default=0)
    signals: int = Field(ge=0, le=30, default=0)
    engagement: int = Field(ge=0, le=10, default=0)
    strategic: int = Field(ge=0, le=10, default=0)

    @property
    def total(self) -> int:
        return self.firmographic + self.capacity + self.signals + self.engagement + self.strategic


class ScoringInput(BaseModel):
    account: Account
    signals: list[Signal] = Field(default_factory=list)
    """Optional enriched signals (with evidence). v0 falls back to account.signals_at_capture."""


class FitScore(BaseModel):
    """Scoring output contract — every field must be UI-renderable for sales.

    The override fields short-circuit the numeric tier (e.g. anti-fit account from
    `disqualification_notes` is capped at C regardless of points).
    """

    account_name: str
    breakdown: ScoreBreakdown
    total: int = Field(ge=0, le=100)
    tier: Tier
    reasons: list[str] = Field(default_factory=list)
    overrides: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"
    """`low` when key dimensions (firmographic, capacity) lack enriched data."""
