"""Smoke tests for the schemas — make sure Pydantic models round-trip and the
tier helpers behave."""

from schemas.account import Account
from schemas.scoring import (
    FitScore,
    ScoreBreakdown,
    Tier,
    tier_distance,
    tier_from_total,
)
from schemas.signal import IndustrialSignalType, SignalAtCapture


def test_tier_from_total_thresholds():
    assert tier_from_total(100) == "AAA"
    assert tier_from_total(80) == "AAA"
    assert tier_from_total(79) == "AA"
    assert tier_from_total(65) == "AA"
    assert tier_from_total(64) == "A"
    assert tier_from_total(50) == "A"
    assert tier_from_total(49) == "B"
    assert tier_from_total(35) == "B"
    assert tier_from_total(34) == "C"
    assert tier_from_total(0) == "C"


def test_tier_distance():
    assert tier_distance("AAA", "AAA") == 0
    assert tier_distance("AAA", "AA") == 1
    assert tier_distance("AAA", "C") == 4
    assert tier_distance("C", "AAA") == 4


def test_account_minimal_construction():
    acc = Account(name="Test AB", country="SE")
    assert acc.name == "Test AB"
    assert acc.country == "SE"
    assert acc.management_priority is False
    assert acc.signals_at_capture == []


def test_account_with_signals():
    acc = Account(
        name="Axido",
        country="SE",
        signals_at_capture=[
            SignalAtCapture(
                type=IndustrialSignalType.PLANT_EXPANSION,
                detail="+1500 kvm nya lokaler",
            ),
            SignalAtCapture(
                type=IndustrialSignalType.HIRE_PRODUCTION_MANAGER,
                detail="Produktionschef med Mazak-bakgrund",
            ),
        ],
    )
    assert len(acc.signals_at_capture) == 2
    assert acc.signals_at_capture[0].type == IndustrialSignalType.PLANT_EXPANSION


def test_score_breakdown_total():
    b = ScoreBreakdown(firmographic=30, capacity=15, signals=25, engagement=5, strategic=8)
    assert b.total == 83


def test_fit_score_round_trip():
    score = FitScore(
        account_name="ACC Innovation",
        breakdown=ScoreBreakdown(firmographic=30, capacity=0, signals=16, engagement=0, strategic=6),
        total=52,
        tier="AAA",
        reasons=["+10 p: CAPEX 125 MSEK", "+3 p: CEO-flaggad"],
        overrides=["FLOOR = AAA: management_priority"],
        confidence="medium",
    )
    blob = score.model_dump_json()
    restored = FitScore.model_validate_json(blob)
    assert restored.tier == "AAA"
    assert restored.account_name == "ACC Innovation"
