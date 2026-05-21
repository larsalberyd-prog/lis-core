"""Discovery agent — promotes ClassifiedSignals to Account stubs.

Pipeline per signal:
  1. Extract employer-name (+ orgnr if present) from the originating event.
  2. Match against `known_account_names` (case-insensitive, alias-aware) →
     if known, attach signal to existing Account (no new stub).
  3. Match against anti-portfolio keywords (FC Maskin, Masentia etc) → reject.
  4. Resolve employer via injected resolver:
       - NO + orgnr → brreg.lookup_by_orgnr
       - NO + name  → brreg.search_by_name (best NACE-match)
       - SE         → JobTech employer-fält direkt (orgnr + name + workplace)
  5. NACE-filter: prefix in CNC_NACE_PREFIXES → keep. Unknown NACE → keep with
     low confidence (enrichment agent can downgrade). Non-CNC NACE → reject.
  6. Build minimal Account with country, geo, segment-hint, signals_at_capture
     containing the original `SignalAtCapture` mapped from the Signal.

Resolver är injekterbar för tester. Default-impl träffar live Brreg.
"""

from __future__ import annotations

from typing import Iterable

from schemas.account import Account
from schemas.signal import SignalAtCapture

from .schema import (
    DiscoveredAccount,
    DiscoveryAgentInput,
    DiscoveryAgentOutput,
    ResolvedCompany,
    Resolver,
)
from agents.signals.schema import ClassifiedSignal


# --- Helpers ---------------------------------------------------------------


def _normalize(name: str) -> str:
    """Strip trailing AB / AS / ASA / Aktiebolag etc and lower-case for matching."""
    suffixes = (" ab", " a/s", " asa", " as", " oy", " aktiebolag", " bolag", " kb", " hb")
    n = name.strip().lower()
    for suf in suffixes:
        if n.endswith(suf):
            n = n[: -len(suf)].strip()
            break
    return n


def _matches_known(name: str, known: Iterable[str]) -> str | None:
    target = _normalize(name)
    for k in known:
        if _normalize(k) == target:
            return k
    return None


def _matches_anti_portfolio(name: str, anti: Iterable[str]) -> str | None:
    lname = name.lower()
    for kw in anti:
        if kw.lower() in lname:
            return kw
    return None


def _country_from_source(source_type: str) -> str:
    if source_type == "nav_pam_stilling" or source_type == "brreg":
        return "NO"
    if source_type == "jobtech" or source_type == "allabolag":
        return "SE"
    return "SE"


def _signal_to_capture(cs: ClassifiedSignal) -> SignalAtCapture:
    """Map en evidence-bunden Signal → SignalAtCapture för Account-stuben.

    Vi tappar lite bevisningskedjan här (`Signal.evidence[]`) men det är
    avsiktligt: SignalAtCapture är "captured at intake" — full evidence-chain
    förblir tillgänglig på den ursprungliga `Signal`-instansen i orchestrator-
    bufferten. Enrichment-agenten kopplar ihop dem igen.
    """
    first_evidence = cs.signal.evidence[0] if cs.signal.evidence else None
    return SignalAtCapture(
        type=cs.signal.signal_type,
        detail=(first_evidence.source_quote[:200] if first_evidence else ""),
        date=cs.signal.detected_at.date().isoformat() if cs.signal.detected_at else None,
        source_hint=(first_evidence.source_type if first_evidence else None),
    )


def _default_resolver(name: str | None, orgnr: str | None, country: str) -> ResolvedCompany | None:
    """Live resolver. Treffar Brreg för NO. Lazy-imports så tester slipper httpx."""
    if country == "NO" and (orgnr or name):
        from shared.integrations import brreg

        if orgnr:
            enhet = brreg.lookup_by_orgnr(orgnr)
        else:
            hits = brreg.search_by_name(name or "", limit=1)
            enhet = hits[0] if hits else None
        if enhet is None:
            return None
        return ResolvedCompany(
            name=enhet.name,
            orgnr=enhet.orgnr,
            country="NO",
            municipality=enhet.municipality,
            nace_code=enhet.nace_code,
            nace_text=enhet.nace_text,
            employees=enhet.employees,
            is_cnc_relevant=enhet.is_cnc_relevant,
            source_url=enhet.source_url,
        )
    if country == "SE" and name:
        # SE: ingen automatisk Brreg-motsvarighet. Vi använder JobTech-employer-
        # fält som-de-är. Enrichment-agenten gör Allabolag-scrape senare.
        return ResolvedCompany(name=name, orgnr=orgnr, country="SE", is_cnc_relevant=True)
    return None


# --- Run -------------------------------------------------------------------


def _build_account(cs: ClassifiedSignal, resolved: ResolvedCompany) -> Account:
    return Account(
        name=resolved.name,
        country=resolved.country,
        geo=resolved.municipality,
        sources=["discovery-agent"],
        signals_at_capture=[_signal_to_capture(cs)],
    )


def run(input: DiscoveryAgentInput, *, resolver: Resolver | None = None) -> DiscoveryAgentOutput:
    out = DiscoveryAgentOutput()
    use_resolver: Resolver = resolver or _default_resolver  # type: ignore[assignment]

    for cs in input.classified_signals:
        employer = cs.employer_name
        if not employer:
            out.discovered.append(
                DiscoveredAccount(
                    decision="rejected_missing_employer",
                    rejection_reason="signal saknar employer_name",
                    classified_signal=cs,
                )
            )
            continue

        # 1. Anti-portfolio gate
        hit = _matches_anti_portfolio(employer, input.anti_portfolio_keywords)
        if hit:
            out.discovered.append(
                DiscoveredAccount(
                    decision="rejected_anti_portfolio",
                    rejection_reason=f"matchar anti-portfolio: {hit}",
                    classified_signal=cs,
                )
            )
            continue

        # 2. Known-account match
        known = _matches_known(employer, input.known_account_names)
        if known:
            out.discovered.append(
                DiscoveredAccount(
                    decision="known_account",
                    matched_known_name=known,
                    classified_signal=cs,
                )
            )
            continue

        # 3. Resolve via integrations
        country = _country_from_source(cs.signal.evidence[0].source_type if cs.signal.evidence else "")
        orgnr = None  # Future: extract from cs.signal raw payload when threaded through
        resolved = use_resolver(employer, orgnr, country)
        if resolved is None:
            out.discovered.append(
                DiscoveredAccount(
                    decision="rejected_missing_employer",
                    rejection_reason=f"resolver hittade ingen post för '{employer}' ({country})",
                    classified_signal=cs,
                )
            )
            continue

        # 4. NACE / CNC-relevance gate
        if not resolved.is_cnc_relevant:
            out.discovered.append(
                DiscoveredAccount(
                    decision="rejected_not_cnc",
                    rejection_reason=(
                        f"NACE {resolved.nace_code} ({resolved.nace_text}) ej CNC-relevant"
                        if resolved.nace_code
                        else "CNC-relevans okänd, default reject"
                    ),
                    classified_signal=cs,
                )
            )
            continue

        # 5. Build new Account stub
        account = _build_account(cs, resolved)
        out.discovered.append(
            DiscoveredAccount(decision="new_account", account=account, classified_signal=cs)
        )

    return out
