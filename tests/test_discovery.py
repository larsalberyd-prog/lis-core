"""Discovery agent — deterministic tests med fake resolver (ingen Brreg-call)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents.discovery.agent import run as discovery_run
from agents.discovery.schema import (
    DiscoveryAgentInput,
    ResolvedCompany,
)
from agents.signals.agent import classify_event
from agents.signals.schema import ClassifiedSignal, RawEvent
from schemas.evidence import Evidence
from schemas.signal import IndustrialSignalType, Signal


# --- Helpers ---------------------------------------------------------------


def _make_signal(
    text: str,
    *,
    source_type: str = "jobtech",
    employer: str | None = "Brunvoll AS",
    source_url: str = "https://example.com/ad/1",
) -> ClassifiedSignal:
    event = RawEvent(
        source_type=source_type,
        source_url=source_url,
        text=text,
        employer_name=employer,
    )
    hits = classify_event(event)
    assert hits, f"signals-agent producerade inget för: {text!r}"
    return hits[0]


def _fake_resolver_brunvoll(name: str | None, orgnr: str | None, country: str):
    return ResolvedCompany(
        name="Brunvoll AS",
        orgnr="911223344",
        country="NO",
        municipality="Molde",
        nace_code="28.110",
        nace_text="Produksjon av motorer og turbiner",
        employees=400,
        is_cnc_relevant=True,
        source_url="https://virksomhet.brreg.no/nb/oppslag/enheter/911223344",
    )


def _fake_resolver_not_cnc(name: str | None, orgnr: str | None, country: str):
    return ResolvedCompany(
        name=name or "Some NonCNC",
        country=country,
        nace_code="47.190",  # Detaljhandel
        nace_text="Detaljhandel med bredt sortiment",
        is_cnc_relevant=False,
    )


def _fake_resolver_none(name, orgnr, country):
    return None


# --- Tests -----------------------------------------------------------------


def test_new_no_account_passes_nace_gate():
    cs = _make_signal(
        "Brunvoll søker dreier med erfaring fra CNC-styrte maskiner. Molde.",
        source_type="nav_pam_stilling",
        employer="Brunvoll AS",
    )
    out = discovery_run(
        DiscoveryAgentInput(classified_signals=[cs]),
        resolver=_fake_resolver_brunvoll,
    )
    assert len(out.discovered) == 1
    assert out.discovered[0].decision == "new_account"
    acc = out.discovered[0].account
    assert acc is not None
    assert acc.name == "Brunvoll AS"
    assert acc.country == "NO"
    assert acc.geo == "Molde"
    assert len(acc.signals_at_capture) == 1
    assert acc.signals_at_capture[0].type == IndustrialSignalType.HIRE_CNC_ROLE


def test_known_account_does_not_create_stub():
    cs = _make_signal(
        "Vrea Mek söker produktionschef till verkstaden i Värnamo.",
        source_type="jobtech",
        employer="Vrea Mek AB",
    )
    out = discovery_run(
        DiscoveryAgentInput(
            classified_signals=[cs],
            known_account_names=["Vrea Mek", "ACC Innovation"],
        ),
        resolver=_fake_resolver_brunvoll,  # shouldn't be called
    )
    assert len(out.discovered) == 1
    d = out.discovered[0]
    assert d.decision == "known_account"
    assert d.matched_known_name == "Vrea Mek"
    assert d.account is None
    assert out.new_accounts == []


def test_anti_portfolio_keyword_blocks_stub():
    cs = _make_signal(
        "FC Maskin AB söker CNC-operatör för EDM-maskiner.",
        source_type="jobtech",
        employer="FC Maskin AB",
    )
    out = discovery_run(
        DiscoveryAgentInput(
            classified_signals=[cs],
            anti_portfolio_keywords=["FC Maskin", "Masentia"],
        ),
        resolver=_fake_resolver_brunvoll,
    )
    assert len(out.discovered) == 1
    assert out.discovered[0].decision == "rejected_anti_portfolio"
    assert "FC Maskin" in (out.discovered[0].rejection_reason or "")


def test_nace_filter_rejects_non_cnc_employer():
    cs = _make_signal(
        "Some NonCNC AS søker CNC-operatør til lager.",
        source_type="nav_pam_stilling",
        employer="Some NonCNC AS",
    )
    out = discovery_run(
        DiscoveryAgentInput(classified_signals=[cs]),
        resolver=_fake_resolver_not_cnc,
    )
    assert len(out.discovered) == 1
    assert out.discovered[0].decision == "rejected_not_cnc"


def test_missing_employer_is_rejected():
    cs = ClassifiedSignal(
        signal=Signal(
            signal_type=IndustrialSignalType.HIRE_CNC_ROLE,
            points_awarded=4,
            evidence=[
                Evidence(
                    source_quote="Vi söker CNC-operatör till okänd arbetsgivare.",
                    extracted_at=datetime.now(timezone.utc),
                    source_type="jobtech",
                )
            ],
            detected_at=datetime.now(timezone.utc),
        ),
        employer_name=None,
    )
    out = discovery_run(
        DiscoveryAgentInput(classified_signals=[cs]),
        resolver=_fake_resolver_brunvoll,
    )
    assert out.discovered[0].decision == "rejected_missing_employer"


def test_resolver_returns_none_is_rejected():
    cs = _make_signal(
        "Ukjent firma AS søker CNC-operatør.",
        source_type="nav_pam_stilling",
        employer="Ukjent firma AS",
    )
    out = discovery_run(
        DiscoveryAgentInput(classified_signals=[cs]),
        resolver=_fake_resolver_none,
    )
    assert out.discovered[0].decision == "rejected_missing_employer"


def test_se_jobtech_uses_employer_field_without_brreg():
    """SE-spåret kräver inte resolver-träff utöver namnet — JobTech-fält räcker
    tills enrichment-agenten lägger till Allabolag."""
    cs = _make_signal(
        "Tooltec AB söker 5 nya CNC-operatörer till Östersund.",
        source_type="jobtech",
        employer="Tooltec AB",
    )
    out = discovery_run(
        DiscoveryAgentInput(classified_signals=[cs]),
        # default resolver path for SE returns is_cnc_relevant=True with just name
        resolver=None,
    )
    assert out.discovered[0].decision == "new_account"
    assert out.discovered[0].account is not None
    assert out.discovered[0].account.country == "SE"
    assert out.discovered[0].account.signals_at_capture[0].type == IndustrialSignalType.HIRE_CNC_OPERATORS_BATCH
