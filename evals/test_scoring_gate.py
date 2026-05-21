"""pytest CI gate — fails the build if scoring drifts below threshold.

Run via: pytest evals/
"""

from __future__ import annotations

import pytest

from evals.runner import DEFAULT_THRESHOLD, run_all


@pytest.fixture(scope="session")
def eval_results():
    return run_all(strict=False)


def test_pass_rate_meets_threshold(eval_results):
    cases, pass_rate = eval_results
    assert pass_rate >= DEFAULT_THRESHOLD, (
        f"Eval pass-rate {pass_rate:.1%} below gate {DEFAULT_THRESHOLD:.0%} — "
        f"{sum(1 for c in cases if not c.passed)}/{len(cases)} fixtures missed."
    )


def test_acc_innovation_is_aaa(eval_results):
    """Fixture #1: ACC Innovation is CEO-flagged. Must score AAA — non-negotiable."""
    cases, _ = eval_results
    acc = next((c for c in cases if c.name == "ACC Innovation"), None)
    assert acc is not None, "ACC Innovation fixture missing"
    assert acc.predicted == "AAA", f"ACC Innovation must be AAA, got {acc.predicted}"


def test_volvo_koping_capped_at_b(eval_results):
    """Anti-fit fixture: Volvo Köping has personal blocker (anti-Mazak teknikchef).
    Must NOT rank above B."""
    cases, _ = eval_results
    volvo = next((c for c in cases if c.name == "Volvo Köping"), None)
    assert volvo is not None, "Volvo Köping fixture missing"
    assert volvo.predicted in ("B", "C"), (
        f"Volvo Köping must be capped at B/C (disqualification), got {volvo.predicted}"
    )


def test_vrea_mek_active_dialogue_is_aaa(eval_results):
    """Active sales dialogue must trump everything else → AAA."""
    cases, _ = eval_results
    vrea = next((c for c in cases if c.name == "Vrea Mek"), None)
    assert vrea is not None, "Vrea Mek fixture missing"
    assert vrea.predicted == "AAA", f"Vrea Mek (active dialogue) must be AAA, got {vrea.predicted}"
