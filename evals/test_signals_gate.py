"""pytest gate for the signals classifier. Mirrors test_scoring_gate.py."""

from __future__ import annotations

import pytest

from evals.signals_runner import DEFAULT_THRESHOLD, run_all


@pytest.fixture(scope="session")
def signal_results():
    return run_all()


def test_signals_pass_rate_meets_threshold(signal_results):
    cases, pass_rate = signal_results
    misses = [c.case_id for c in cases if not c.passed]
    assert pass_rate >= DEFAULT_THRESHOLD, (
        f"Signals pass-rate {pass_rate:.1%} below gate {DEFAULT_THRESHOLD:.0%} — "
        f"missed: {misses}"
    )


def test_negative_cases_emit_nothing(signal_results):
    """No-signal cases (receptionist, generic säljare, capex without production
    noun) MUST emit zero signals. False positives are the worst kind — they
    pollute the säljar-list."""
    cases, _ = signal_results
    for case in cases:
        if case.case_id.startswith("no_signal_") or case.case_id == "capex_money_no_production_noun":
            assert case.predicted == [], (
                f"Negative case {case.case_id} emitted unexpected signals: {case.predicted}"
            )


def test_batch_hire_supersedes_singletons(signal_results):
    """5 CNC-operatörer must classify as HIRE_CNC_OPERATORS_BATCH, not HIRE_CNC_ROLE."""
    cases, _ = signal_results
    batch = next((c for c in cases if c.case_id == "batch_cnc_hire_se"), None)
    assert batch is not None
    assert batch.predicted == ["HIRE_CNC_OPERATORS_BATCH"], (
        f"Batch hire wrongly classified: {batch.predicted}"
    )


def test_capex_requires_production_noun(signal_results):
    """Money mention WITHOUT a production noun nearby must NOT trigger CAPEX_ANNOUNCEMENT."""
    cases, _ = signal_results
    case = next((c for c in cases if c.case_id == "capex_money_no_production_noun"), None)
    assert case is not None
    assert "CAPEX_ANNOUNCEMENT" not in case.predicted, (
        f"False-positive CAPEX without production context: {case.predicted}"
    )
