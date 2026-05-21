"""Demo-export: kör scoring-agenten på demo_fixtures.yaml och producera
companies.json i frontend-format (ravema-lis/client/src/data/companies.json).

Användning:
    python3 -m demo.export_demo_accounts

Mappar mellan:
  - demo_fixtures.yaml  (hand-curaterad pilot-input, läsbar för Klas+Nejra)
  - lis-core schemas    (Account, SignalAtCapture)
  - frontend Company    (typad i useCompanies.ts)

Custom signal-typer som inte finns i IndustrialSignalType enum hanteras genom
att mappa till Account-fält istället för att skapa fake Signal-poster:
  - MANAGEMENT_PRIORITY        → account.management_priority = True
  - COMPETITOR_INCUMBENT_KNOWN → account.competitor_incumbent (rendreras i UI)
  - DISQUALIFICATION_BLOCKER   → account.disqualification_notes
  - PROCUREMENT_GATE_BLOCKER   → appended to disqualification_notes
  - CUSTOMER_WIN_DOWNSTREAM    → MAJOR_CUSTOMER_WIN

Inga LLM-anrop görs (scoring kör Mode B deterministiskt).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

# Path-bootstrapping: skriptet körs som modul från lis-core/
HERE = Path(__file__).resolve().parent
LIS_CORE = HERE.parent
PROJECT_ROOT = LIS_CORE.parent
FRONTEND_JSON = PROJECT_ROOT / "ravema-lis" / "client" / "src" / "data" / "companies.json"

sys.path.insert(0, str(LIS_CORE))

from agents.scoring.agent import run as score_account  # noqa: E402
from schemas.account import Account, DecisionMaker, RelationshipBlocker  # noqa: E402
from schemas.scoring import FitScore, ScoringInput  # noqa: E402
from schemas.signal import IndustrialSignalType, SignalAtCapture  # noqa: E402

# --- Custom → canonical signal mapping ---------------------------------------

CUSTOM_SIGNAL_REMAP: dict[str, IndustrialSignalType | None] = {
    "CUSTOMER_WIN_DOWNSTREAM": IndustrialSignalType.MAJOR_CUSTOMER_WIN,
    "MANAGEMENT_PRIORITY": None,  # → account.management_priority
    "COMPETITOR_INCUMBENT_KNOWN": None,  # → account.competitor_incumbent
    "COMPETITOR_DISPLACEMENT_TARGET": None,  # → triggers UI only
    "DISQUALIFICATION_BLOCKER": None,  # → account.disqualification_notes
    "PROCUREMENT_GATE_BLOCKER": None,  # → appended disqualification
}


def _build_account(fx: dict[str, Any]) -> Account:
    signals_at_capture: list[SignalAtCapture] = []
    management_priority = False
    competitor_incumbent: str | None = None
    disqualification_parts: list[str] = []
    relationship_blockers: list[RelationshipBlocker] = []

    for sig in fx.get("signals", []):
        raw_type = sig["type"]

        # Canonical enum value?
        try:
            enum_type = IndustrialSignalType(raw_type)
            signals_at_capture.append(
                SignalAtCapture(
                    type=enum_type,
                    detail=sig.get("detail", ""),
                    date=sig.get("date"),
                    source_hint=sig.get("evidence_source"),
                )
            )
            continue
        except ValueError:
            pass

        # Custom → remap or sideband
        if raw_type in CUSTOM_SIGNAL_REMAP:
            target = CUSTOM_SIGNAL_REMAP[raw_type]
            if target is not None:
                signals_at_capture.append(
                    SignalAtCapture(
                        type=target,
                        detail=sig.get("detail", ""),
                        date=sig.get("date"),
                        source_hint=sig.get("evidence_source"),
                    )
                )
            else:
                # Sideband to account-level field
                if raw_type == "MANAGEMENT_PRIORITY":
                    management_priority = True
                elif raw_type == "COMPETITOR_INCUMBENT_KNOWN":
                    competitor_incumbent = sig.get("detail")
                elif raw_type in ("DISQUALIFICATION_BLOCKER", "PROCUREMENT_GATE_BLOCKER"):
                    disqualification_parts.append(sig.get("detail", ""))
            continue

        # Unknown signal type — surface loudly so we catch typos
        raise ValueError(f"Unknown signal type {raw_type!r} in {fx['name']}")

    # disqualification_notes from fixture YAML
    for note in fx.get("disqualification_notes", []) or []:
        disqualification_parts.append(note)

    # Decision makers — convert "search:" entries to placeholder name
    known_dms: list[DecisionMaker] = []
    for dm in fx.get("decision_makers", []) or []:
        if "name" in dm:
            known_dms.append(DecisionMaker(name=dm["name"], role=dm.get("role")))

    # Personal relationship blockers (anti-fit)
    for dm in fx.get("decision_makers", []) or []:
        if "search" in dm and dm.get("note") and "blocker" in dm.get("note", "").lower():
            relationship_blockers.append(
                RelationshipBlocker(
                    role=dm.get("search", "unknown"),
                    history=None,
                    attitude="anti-Mazak" if "anti-Mazak" in dm.get("note", "") else None,
                )
            )
    # Also from raw fixture "personal_relationship_blockers"
    for blk in fx.get("personal_relationship_blockers", []) or []:
        relationship_blockers.append(RelationshipBlocker(**blk))

    # Sources — derive from raw fixture (not post-filter signal list, so custom
    # signal types like COMPETITOR_DISPLACEMENT_TARGET still count as Nejra-input)
    sources: list[str] = []
    if fx.get("rationale_klas"):
        sources.append("klas-excel-2026-05-20")
    if fx.get("signals"):
        sources.append("nejra-email-2026-05-20")
    if not sources:
        sources.append("icp-md-anchors")

    return Account(
        name=fx["name"],
        aliases=fx.get("aliases", []),
        country=fx.get("country", "SE"),
        geo=fx.get("city") or fx.get("geo"),
        segment=fx.get("segment"),
        district=fx.get("district"),
        salesperson=fx.get("salesperson_assigned"),
        sources=sources,
        salesperson_rationale=fx.get("rationale_klas") or fx.get("description"),
        known_decision_makers=known_dms,
        competitor_incumbent=competitor_incumbent or fx.get("competitor_incumbent"),
        suggested_entry_angle=(fx.get("entry_angles") or [None])[0],
        management_priority=management_priority,
        signals_at_capture=signals_at_capture,
        disqualification_notes="; ".join(disqualification_parts) if disqualification_parts else None,
        personal_relationship_blockers=relationship_blockers,
    )


# --- Frontend Company mapping -------------------------------------------------


def _decision_makers_to_frontend(fx: dict[str, Any]) -> list[dict[str, Any]]:
    """Map demo decision_makers → frontend DecisionMaker shape.

    For "search:" entries (placeholder for Nejra to enrich), we render
    "Sök: <role>" as name with no email. For named entries we keep name.
    """
    domain_hint = fx["name"].lower().replace(" ", "")  # naive — for demo only
    out: list[dict[str, Any]] = []
    for dm in fx.get("decision_makers", []) or []:
        if "name" in dm:
            out.append(
                {
                    "name": dm["name"],
                    "title": dm.get("title", ""),
                    "role": dm.get("role", "Other"),
                    "priority": dm.get("priority", "medium"),
                    "email": None,
                    "phone": None,
                    "linkedin": None,
                    "email_patterns": [
                        f"förnamn.efternamn@{domain_hint}.se",
                        f"f.efternamn@{domain_hint}.se",
                    ],
                    "linkedin_search": (
                        "https://www.linkedin.com/search/results/people/?"
                        f"keywords={dm['name'].replace(' ', '%20')}%20{fx['name'].replace(' ', '%20')}"
                    ),
                    "note": dm.get("note"),
                }
            )
        elif "search" in dm:
            out.append(
                {
                    "name": f"Sök: {dm['search']}",
                    "title": dm["search"],
                    "role": dm.get("role", "Other"),
                    "priority": dm.get("priority", "medium"),
                    "email": None,
                    "phone": None,
                    "linkedin": None,
                    "email_patterns": [
                        f"förnamn.efternamn@{domain_hint}.se",
                        f"f.efternamn@{domain_hint}.se",
                    ],
                    "note": dm.get("note"),
                }
            )
    return out


def _account_to_company(fx: dict[str, Any], score: FitScore, account: Account) -> dict[str, Any]:
    notes_parts: list[str] = []
    if account.salesperson_rationale:
        notes_parts.append(f"Rationale: {account.salesperson_rationale.strip()}")
    if account.disqualification_notes:
        notes_parts.append(f"Disqualification: {account.disqualification_notes}")
    if score.overrides:
        notes_parts.append("Score overrides: " + " | ".join(score.overrides))

    return {
        "id": fx["id"],
        "name": fx["name"],
        "country": {"SE": "Sverige", "NO": "Norge", "DK": "Danmark", "FI": "Finland"}.get(
            fx.get("country", "SE"), fx.get("country", "SE")
        ),
        "city": fx.get("city") or fx.get("geo") or "",
        "segment": fx.get("segment") or "",
        "priority": score.tier,
        "status": "new",
        "assignedTo": fx.get("salesperson_assigned"),
        "deadline": None,
        "description": fx.get("description", "").strip(),
        "sowPotential": fx.get("sow_potential", ""),
        "triggers": fx.get("triggers", []) or [],
        "decisionMakers": _decision_makers_to_frontend(fx),
        "entryAngles": fx.get("entry_angles", []) or [],
        "qualifyingQuestions": fx.get("qualifying_questions", []) or [],
        "notes": "\n\n".join(notes_parts) if notes_parts else None,
        "nextSteps": None,
        "qualifierAnswers": [],
        # Ravema-LIS extension fields (rendered by reskinned components)
        "lis": {
            "tier": score.tier,
            "scoreTotal": score.total,
            "scoreBreakdown": score.breakdown.model_dump(),
            "reasons": score.reasons,
            "overrides": score.overrides,
            "confidence": score.confidence,
            "signals": [
                {
                    "type": s["type"],
                    "detail": s.get("detail", ""),
                    "date": s.get("date"),
                    "evidenceSource": s.get("evidence_source"),
                }
                for s in fx.get("signals", []) or []
            ],
            "district": fx.get("district"),
            "competitorIncumbent": account.competitor_incumbent,
            "managementPriority": account.management_priority,
            "rationaleKlas": fx.get("rationale_klas", "").strip(),
        },
    }


# --- Main --------------------------------------------------------------------


def main() -> None:
    fixtures_path = HERE / "demo_fixtures.yaml"
    with fixtures_path.open() as f:
        data = yaml.safe_load(f)

    companies: list[dict[str, Any]] = []
    print(f"Loaded {len(data['accounts'])} demo accounts from {fixtures_path.name}")
    print(f"{'Konto':30s} {'Förvänt':<6s} {'Score':<7s} {'Tier':<5s} Confidence")
    print("-" * 70)

    for fx in data["accounts"]:
        account = _build_account(fx)
        result = score_account(ScoringInput(account=account))
        company = _account_to_company(fx, result, account)
        companies.append(company)

        expected = fx.get("expected_tier", "?")
        marker = "✓" if result.tier == expected else "≠"
        print(
            f"{fx['name']:30s} {expected:<6s} {result.total:<7d} "
            f"{result.tier:<5s} {result.confidence}  {marker}"
        )

    FRONTEND_JSON.parent.mkdir(parents=True, exist_ok=True)
    with FRONTEND_JSON.open("w") as f:
        json.dump(companies, f, ensure_ascii=False, indent=2)

    print(f"\nWrote {len(companies)} accounts → {FRONTEND_JSON.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
