"""Scoring agent I/O contract — thin wrappers over shared schemas/scoring.py."""

from __future__ import annotations

from pydantic import BaseModel

from schemas.scoring import FitScore, ScoringInput


class ScoringAgentInput(ScoringInput):
    """Re-export with explicit name for the agent entry point."""


class ScoringAgentOutput(BaseModel):
    score: FitScore
