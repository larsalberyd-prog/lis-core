"""Signals agent — classify raw events into typed IndustrialSignals.

Two-stage pipeline:
  1. Deterministic keyword classifier (this file). No LLM. Runs in CI without
     ANTHROPIC_API_KEY. Catches >80% of well-known patterns ("vi söker
     produktionschef", "investerar 50 MSEK i ny produktionslina") cheap and
     reproducibly.
  2. LLM fallback (opt-in via `use_llm=True`). Only invoked for events the
     deterministic pass left untouched. Output gates through the same
     evidence substring-check as everything else.

The output schema (`Signal`) carries the score points directly so the
scoring agent can consume signals without redoing the lookup table.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Iterable

from schemas.evidence import Evidence
from schemas.signal import IndustrialSignalType, Signal

from .schema import (
    ClassifiedSignal,
    Confidence,
    RawEvent,
    SignalsAgentInput,
    SignalsAgentOutput,
)

# --- Keyword tables --------------------------------------------------------
#
# Curated from Klas+Nejra ICP examples (Vrea Mek, ACC Innovation), Heidenhain
# prior art and ICP.md anchor messaging. Bias toward precision over recall —
# false positives are noise in a säljar-facing list.

_SE_HIRE_TITLES: dict[str, IndustrialSignalType] = {
    r"\bproduktionschef": IndustrialSignalType.HIRE_PRODUCTION_MANAGER,
    r"\boperations? director": IndustrialSignalType.HIRE_OPERATIONS_DIRECTOR,
    r"\boperativ chef": IndustrialSignalType.HIRE_OPERATIONS_DIRECTOR,
    r"\bproduktionsledare": IndustrialSignalType.HIRE_PRODUCTION_MANAGER,
    r"\bproduktionstekniker": IndustrialSignalType.HIRE_MANUFACTURING_ENGINEER,
    r"\bberedare": IndustrialSignalType.HIRE_MANUFACTURING_ENGINEER,
    r"\bafter[\s-]?sales\s*manager": IndustrialSignalType.HIRE_AFTER_SALES_MANAGER,
    r"\beftermarknadschef": IndustrialSignalType.HIRE_AFTER_SALES_MANAGER,
    r"\bCNC[- ]?operatör": IndustrialSignalType.HIRE_CNC_ROLE,
    r"\bfleroperationssvarvare": IndustrialSignalType.HIRE_CNC_ROLE,
    r"\bfräsare": IndustrialSignalType.HIRE_CNC_ROLE,
    r"\bsvarvare": IndustrialSignalType.HIRE_CNC_ROLE,
    r"\bCNC[- ]?tekniker": IndustrialSignalType.HIRE_CNC_ROLE,
    r"\bCNC[- ]?programmerare": IndustrialSignalType.HIRE_CNC_ROLE,
}

_NO_HIRE_TITLES: dict[str, IndustrialSignalType] = {
    r"\bprodukjonsleder": IndustrialSignalType.HIRE_PRODUCTION_MANAGER,
    r"\bproduksjonsleder": IndustrialSignalType.HIRE_PRODUCTION_MANAGER,
    r"\bproduksjonssjef": IndustrialSignalType.HIRE_PRODUCTION_MANAGER,
    r"\bdriftssjef": IndustrialSignalType.HIRE_OPERATIONS_DIRECTOR,
    r"\bdreier\b": IndustrialSignalType.HIRE_CNC_ROLE,
    r"\bCNC[- ]?operatør": IndustrialSignalType.HIRE_CNC_ROLE,
    r"\bmaskineringsoperatør": IndustrialSignalType.HIRE_CNC_ROLE,
    r"\bverktøymaker": IndustrialSignalType.HIRE_CNC_ROLE,
    r"\bautomasjonsingeniør": IndustrialSignalType.HIRE_MANUFACTURING_ENGINEER,
}

_HIRE_TITLES: dict[str, IndustrialSignalType] = {**_SE_HIRE_TITLES, **_NO_HIRE_TITLES}

# CAPEX patterns — looks for explicit money + production noun nearby.
_CAPEX_MONEY = re.compile(
    r"(?P<amt>\d{1,3}(?:[\s.,]\d{3})*|\d+)\s*"
    r"(?P<unit>MSEK|MNOK|MEUR|miljoner|millioner|miljon|million|mkr|MSEK\.|MNOK\.)",
    re.IGNORECASE,
)
_CAPEX_PRODUCTION_NOUNS = (
    "produktion",
    "produksjon",
    "fabrik",
    "anläggning",
    "anlegg",
    "ny linje",
    "ny lina",
    "produktionslina",
    "produksjonslinje",
    "maskinpark",
    "ny hall",
    "investerar",
    "investerer",
    "kapacitet",
    "kapasitet",
    "automation",
    "automasjon",
)

_PLANT_EXPANSION = (
    "ny fabrik",
    "ny anläggning",
    "ny anlegg",
    "utökar produktion",
    "utvider produksjon",
    "fördubblar kapacitet",
    "fordobler kapasitet",
    "expanderar i",
    "expanderer i",
    "bygger ny",
    "bygger ut",
    "ny produktionshall",
    "ny verkstedshall",
)

_GEO_EXPANSION = (
    "etablerar i",
    "etablerer seg i",
    "öppnar kontor i",
    "åpner kontor i",
    "öppnar fabrik i",
    "åpner fabrikk i",
)

_CEO_TRANSITION = (
    "ny vd",
    "ny CEO",
    "ny administrerande direktör",
    "ny administrerende direktør",
    "tillträder som vd",
    "tiltrer som administrerende",
)

_OWNERSHIP = (
    "förvärvar",
    "forvarver",
    "kjøper opp",
    "köper upp",
    "blir uppköpta",
    "blir kjøpt opp",
    "majoritetspost",
    "majoritetsandel",
)

_FUNDING = (
    "vinnova",
    "innovasjon norge",
    "tillväxtverket",
    "horizon europe",
    "EU-bidrag",
    "nyemission",
)

_SUPPLIER_SWITCH = (
    "byte av maskinleverantör",
    "bytter maskinleverandør",
    "ny maskinleverantör",
    "ny maskinleverandør",
)

_CUSTOMER_WIN = (
    "vinner kontrakt",
    "vinner avtal",
    "tilldelas ramavtal",
    "tildelt rammeavtale",
    "skriver kontrakt med",
    "har inngått avtale med",
)

# Score points mirror agents/scoring/agent.py _SIGNAL_POINTS. Kept in sync via
# a unit test rather than imported, so neither agent loads the other.
_SIGNAL_POINTS: dict[IndustrialSignalType, int] = {
    IndustrialSignalType.CAPEX_ANNOUNCEMENT: 10,
    IndustrialSignalType.PLANT_EXPANSION: 7,
    IndustrialSignalType.GEOGRAPHIC_EXPANSION: 5,
    IndustrialSignalType.NEW_FACILITY_BUILDING_PERMIT: 3,
    IndustrialSignalType.HIRE_OPERATIONS_DIRECTOR: 8,
    IndustrialSignalType.HIRE_PRODUCTION_MANAGER: 7,
    IndustrialSignalType.HIRE_MANUFACTURING_ENGINEER: 4,
    IndustrialSignalType.HIRE_AFTER_SALES_MANAGER: 6,
    IndustrialSignalType.HIRE_CNC_OPERATORS_BATCH: 8,
    IndustrialSignalType.HIRE_CNC_ROLE: 4,
    IndustrialSignalType.OWNERSHIP_CHANGE: 3,
    IndustrialSignalType.CEO_TRANSITION: 2,
    IndustrialSignalType.OWNERSHIP_GROUP_INVESTMENT: 4,
    IndustrialSignalType.CERTIFICATION_CHANGE: 4,
    IndustrialSignalType.MAJOR_CUSTOMER_WIN: 5,
    IndustrialSignalType.MAJOR_CUSTOMER_LOSS: 3,
    IndustrialSignalType.SUPPLIER_SWITCH_SIGNAL: 6,
    IndustrialSignalType.REGULATORY_PRESSURE: 3,
    IndustrialSignalType.QUARTERLY_REPORT_TRIGGER: 2,
    IndustrialSignalType.PUBLIC_PROCUREMENT_WIN: 5,
    IndustrialSignalType.FUNDING_GRANT: 4,
    IndustrialSignalType.ACTIVE_SALES_DIALOGUE: 15,
}


def _extract_quote(text: str, match_start: int, match_end: int, window: int = 80) -> str:
    """Pull a short context window around a regex hit. Keeps it ≤300 chars."""
    lo = max(0, match_start - window)
    hi = min(len(text), match_end + window)
    quote = text[lo:hi].strip()
    if len(quote) < 10:
        # Pad up to 10 chars; substring-validator requires ≥10.
        quote = text[max(0, match_end - 60) : min(len(text), match_end + 100)].strip()
    return quote[:300]


def _looks_like_batch_hire(text: str, hits: list[tuple[str, IndustrialSignalType, int, int]]) -> bool:
    """≥3 CNC-roles in same ad = batch hire (CAPEX-proxy)."""
    cnc_hits = [h for h in hits if h[1] == IndustrialSignalType.HIRE_CNC_ROLE]
    if len(cnc_hits) >= 3:
        return True
    # Also: "vi söker 5 CNC-operatörer" — digit ≥3 before role term.
    return bool(re.search(r"\b([3-9]|\d{2,})\s*(?:nya\s+)?(?:CNC|svarv|fräs|dreier)", text, re.IGNORECASE))


def _scan_titles(text: str) -> list[tuple[str, IndustrialSignalType, int, int]]:
    """Return all hire-title hits with their match spans."""
    hits: list[tuple[str, IndustrialSignalType, int, int]] = []
    for pattern, sig_type in _HIRE_TITLES.items():
        for m in re.finditer(pattern, text, re.IGNORECASE):
            hits.append((m.group(0), sig_type, m.start(), m.end()))
    return hits


def _scan_capex(text: str) -> tuple[bool, re.Match[str] | None]:
    for m in _CAPEX_MONEY.finditer(text):
        # Need a production noun within 200 chars of the money mention.
        lo = max(0, m.start() - 200)
        hi = min(len(text), m.end() + 200)
        window = text[lo:hi].lower()
        if any(noun in window for noun in _CAPEX_PRODUCTION_NOUNS):
            return True, m
    return False, None


def _scan_phrases(text: str, phrases: Iterable[str]) -> tuple[int, int] | None:
    lower = text.lower()
    for p in phrases:
        idx = lower.find(p.lower())
        if idx >= 0:
            return idx, idx + len(p)
    return None


def _evidence(event: RawEvent, quote: str, now: datetime) -> Evidence:
    return Evidence(
        source_url=event.source_url,
        source_quote=quote,
        extracted_at=event.published_at or now,
        source_type=event.source_type,
    )


def _signal(
    sig_type: IndustrialSignalType,
    event: RawEvent,
    quote: str,
    now: datetime,
) -> Signal:
    return Signal(
        signal_type=sig_type,
        points_awarded=_SIGNAL_POINTS.get(sig_type, 0),
        evidence=[_evidence(event, quote, now)],
        detected_at=now,
    )


def classify_event(event: RawEvent) -> list[ClassifiedSignal]:
    """Run the deterministic classifier on a single event."""
    now = datetime.now(timezone.utc)
    text = event.text
    if not text.strip():
        return []

    results: list[ClassifiedSignal] = []

    title_hits = _scan_titles(text)
    if title_hits:
        if _looks_like_batch_hire(text, title_hits):
            first = title_hits[0]
            quote = _extract_quote(text, first[2], first[3])
            results.append(
                ClassifiedSignal(
                    signal=_signal(IndustrialSignalType.HIRE_CNC_OPERATORS_BATCH, event, quote, now),
                    matched_keywords=[h[0] for h in title_hits],
                    confidence="high",
                    employer_name=event.employer_name,
                )
            )
        else:
            # Emit one signal per unique signal-type. Dedupe — a job ad
            # listing "produktionschef / produktionsledare" should fire once.
            seen_types: set[IndustrialSignalType] = set()
            for kw, sig_type, start, end in title_hits:
                if sig_type in seen_types:
                    continue
                seen_types.add(sig_type)
                quote = _extract_quote(text, start, end)
                results.append(
                    ClassifiedSignal(
                        signal=_signal(sig_type, event, quote, now),
                        matched_keywords=[kw],
                        confidence="high",
                        employer_name=event.employer_name,
                    )
                )

    has_capex, capex_match = _scan_capex(text)
    if has_capex and capex_match is not None:
        quote = _extract_quote(text, capex_match.start(), capex_match.end())
        results.append(
            ClassifiedSignal(
                signal=_signal(IndustrialSignalType.CAPEX_ANNOUNCEMENT, event, quote, now),
                matched_keywords=[capex_match.group(0)],
                confidence="high",
                employer_name=event.employer_name,
            )
        )

    for sig_type, phrases in (
        (IndustrialSignalType.PLANT_EXPANSION, _PLANT_EXPANSION),
        (IndustrialSignalType.GEOGRAPHIC_EXPANSION, _GEO_EXPANSION),
        (IndustrialSignalType.CEO_TRANSITION, _CEO_TRANSITION),
        (IndustrialSignalType.OWNERSHIP_CHANGE, _OWNERSHIP),
        (IndustrialSignalType.FUNDING_GRANT, _FUNDING),
        (IndustrialSignalType.SUPPLIER_SWITCH_SIGNAL, _SUPPLIER_SWITCH),
        (IndustrialSignalType.MAJOR_CUSTOMER_WIN, _CUSTOMER_WIN),
    ):
        span = _scan_phrases(text, phrases)
        if span is None:
            continue
        quote = _extract_quote(text, span[0], span[1])
        results.append(
            ClassifiedSignal(
                signal=_signal(sig_type, event, quote, now),
                matched_keywords=[text[span[0] : span[1]]],
                confidence="medium",
                employer_name=event.employer_name,
            )
        )

    return results


def _llm_classify(event: RawEvent) -> list[ClassifiedSignal]:
    """LLM fallback. Only called when use_llm=True and deterministic was empty."""
    # Lazy import — keeps the deterministic path runnable without anthropic SDK.
    from shared.llm import call_llm, load_prompt
    import os
    from pathlib import Path

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return []

    prompt_path = Path(__file__).parent / "prompt.md"
    system = load_prompt(str(prompt_path))

    user_msg = json.dumps(
        {
            "event_text": event.text,
            "employer_name": event.employer_name,
            "occupation": event.occupation,
            "source_type": event.source_type,
        },
        ensure_ascii=False,
    )

    result = call_llm(
        agent_name="signals",
        system=system,
        messages=[{"role": "user", "content": user_msg}],
        max_tokens=1024,
        temperature=0.0,
    )

    now = datetime.now(timezone.utc)
    try:
        payload = json.loads(result.text)
    except json.JSONDecodeError:
        return []

    classified: list[ClassifiedSignal] = []
    for item in payload.get("classified", []):
        try:
            sig_type = IndustrialSignalType(item["signal_type"])
        except (KeyError, ValueError):
            continue
        quote = (item.get("source_quote") or "").strip()
        if len(quote) < 10 or quote not in event.text:
            # Hallucination — quote not in source. Drop per evidence-mandate.
            continue
        confidence: Confidence = item.get("confidence", "medium")
        classified.append(
            ClassifiedSignal(
                signal=_signal(sig_type, event, quote, now),
                matched_keywords=[],
                confidence=confidence,
                employer_name=event.employer_name,
            )
        )
    return classified


def run(input: SignalsAgentInput) -> SignalsAgentOutput:
    """Classify a batch of raw events. Deterministic first; LLM only if opted in."""
    output = SignalsAgentOutput()
    for event in input.events:
        hits = classify_event(event)
        if not hits and input.use_llm:
            hits = _llm_classify(event)
        if hits:
            output.classified.extend(hits)
        else:
            output.skipped_event_count += 1
    return output
