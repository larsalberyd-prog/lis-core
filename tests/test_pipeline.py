"""End-to-end pipeline smoke test — JobTech-event → signals → discovery → scoring."""

from __future__ import annotations

from agents.discovery.schema import ResolvedCompany
from agents.signals.schema import RawEvent
from orchestrator.pipeline import run_pipeline
from schemas.account import Account
from schemas.signal import IndustrialSignalType


def _resolver_se_only(name, orgnr, country):
    """SE-spår använder JobTech-fält direkt; NO skulle gå mot Brreg."""
    if country != "SE" or not name:
        return None
    return ResolvedCompany(name=name, country="SE", is_cnc_relevant=True)


def test_pipeline_jobtech_vrea_mek_production_manager_to_score():
    """Säljbart end-to-end-case: ny JobTech-annons fångar produktionschef-
    rekrytering hos en NY arbetsgivare → produces stub + score."""
    events = [
        RawEvent(
            source_type="jobtech",
            source_url="https://arbetsformedlingen.se/platsbanken/annonser/e2e-1",
            text=(
                "Stenbergs Maskin i Värnamo söker en erfaren produktionschef "
                "som ska leda vår CNC-verkstad och Mazak INTEGREX-installationer."
            ),
            employer_name="Stenbergs Maskin AB",
        ),
    ]

    result = run_pipeline(
        events,
        known_accounts=[],  # nytt konto
        anti_portfolio_keywords=["FC Maskin", "Masentia"],
        resolver=_resolver_se_only,
    )

    assert result.classified_signal_count == 1
    assert len(result.new_accounts_with_scores) == 1
    account, score = result.new_accounts_with_scores[0]
    assert account.name == "Stenbergs Maskin AB"
    assert account.country == "SE"
    assert account.signals_at_capture[0].type == IndustrialSignalType.HIRE_PRODUCTION_MANAGER
    # Ingen ICP/Klas/Nejra-curation → poäng ligger lågt, men score-objektet existerar
    assert score.account_name == "Stenbergs Maskin AB"
    assert score.tier in ("AAA", "AA", "A", "B", "C")


def test_pipeline_signal_to_known_account_does_not_create_stub():
    """En signal som matchar en redan-känd account → routes till known_account_signals,
    skapar inte ny stub."""
    vrea = Account(name="Vrea Mek", country="SE", aliases=["Vrea Mek AB"])
    events = [
        RawEvent(
            source_type="jobtech",
            source_url="https://arbetsformedlingen.se/platsbanken/annonser/e2e-2",
            text="Vrea Mek AB söker produktionschef till verkstaden i Värnamo.",
            employer_name="Vrea Mek AB",
        ),
    ]
    result = run_pipeline(events, known_accounts=[vrea], resolver=_resolver_se_only)
    assert result.new_accounts_with_scores == []
    assert len(result.known_account_signals) == 1
    assert result.known_account_signals[0].matched_known_name in {"Vrea Mek", "Vrea Mek AB"}


def test_pipeline_anti_portfolio_event_rejected_before_scoring():
    events = [
        RawEvent(
            source_type="jobtech",
            source_url="https://arbetsformedlingen.se/platsbanken/annonser/e2e-3",
            text="FC Maskin söker CNC-operatör till EDM-avdelningen i Lerum.",
            employer_name="FC Maskin AB",
        ),
    ]
    result = run_pipeline(
        events,
        anti_portfolio_keywords=["FC Maskin"],
        resolver=_resolver_se_only,
    )
    assert result.new_accounts_with_scores == []
    assert len(result.rejected) == 1
    assert "FC Maskin" in (result.rejected[0].rejection_reason or "")


def test_pipeline_negative_event_is_skipped():
    """Receptionist-annons producerar inga signaler → discovery anropas inte ens
    för det eventet, och skipped_event_count räknar upp."""
    events = [
        RawEvent(
            source_type="jobtech",
            source_url="https://arbetsformedlingen.se/platsbanken/annonser/e2e-4",
            text="Volvo Cars söker receptionist till huvudkontoret i Torslanda.",
            employer_name="Volvo Cars",
        ),
    ]
    result = run_pipeline(events, resolver=_resolver_se_only)
    assert result.classified_signal_count == 0
    assert result.skipped_event_count == 1
    assert result.new_accounts_with_scores == []
